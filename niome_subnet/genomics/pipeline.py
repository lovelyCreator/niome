"""End-to-end FASTQ -> normalized VCF pipeline for the NIOME miner.

Tight constraint: FORWARD_TIMEOUT = 60s wall-clock to download FASTQs,
align, call variants, normalize, annotate, and respond. Optimized for
RTX 5090 + small CFTR region (~190kb).

Prerequisites on the miner host (Ubuntu 22.04):
  - bwa-mem2, samtools, bcftools, tabix, bgzip on PATH
  - One of:
      (a) Docker + GPU runtime + `google/deepvariant:1.8.0-gpu` pulled (default)
      (b) Clair3 native install (faster startup): `conda install -c bioconda clair3`
          plus a Clair3 model (e.g. ilmn pre-trained from
          https://github.com/HKU-BAL/Clair3#pre-trained-models)
  - Reference FASTA at NIOME_REF_FASTA (GRCh38). Indexes built on first use.

Environment variables:
  NIOME_REF_FASTA          required, absolute path to GRCh38 FASTA
  NIOME_CALLER             "deepvariant" (default) | "clair3" | "bcftools" |
                           "dual" (DV+bcftools) | "strelka" | "triple" (DV+bcftools+Strelka)
  NIOME_DEEPVARIANT_IMAGE  default: google/deepvariant:1.8.0-gpu
  NIOME_DV_MODEL           default: WGS  (WGS | WES | PACBIO | ONT_R104)
  NIOME_STRELKA_IMAGE      default: quay.io/biocontainers/strelka:2.9.10--0
  NIOME_CLAIR3_MODEL_PATH  required if NIOME_CALLER=clair3
  NIOME_CLAIR3_PLATFORM    default: ilmn
  NIOME_MIN_QUAL           default: 20  (filter calls below this QUAL)
  NIOME_GPU                default: "1" (use --gpus all for docker callers)
  NIOME_DBSNP_VCF          path to ClinVar VCF for ID annotation (enables ClinVar-only filter)
"""

import concurrent.futures
import os
import shutil
import subprocess
import tempfile
import time
import urllib.request
from typing import Optional

import bittensor as bt


CALLER = os.environ.get("NIOME_CALLER", "deepvariant").lower()
DBSNP_VCF = os.environ.get("NIOME_DBSNP_VCF")
REF_FASTA = os.environ.get("NIOME_REF_FASTA")
DEEPVARIANT_IMAGE = os.environ.get(
    "NIOME_DEEPVARIANT_IMAGE", "google/deepvariant:1.8.0-gpu"
)
DV_MODEL = os.environ.get("NIOME_DV_MODEL", "WGS")
# GPU device selector for DeepVariant. Default: "device=1" so we don't fight
# with whatever else is using GPU 0 (e.g. a separate subnet 32 miner).
# Set to "all" if you only have one GPU OR want to use both.
DV_GPU_DEVICE = os.environ.get("NIOME_DV_GPU_DEVICE", "device=1")
CLAIR3_MODEL_PATH = os.environ.get("NIOME_CLAIR3_MODEL_PATH")
CLAIR3_PLATFORM = os.environ.get("NIOME_CLAIR3_PLATFORM", "ilmn")
MIN_QUAL = float(os.environ.get("NIOME_MIN_QUAL", "20"))
USE_GPU = os.environ.get("NIOME_GPU", "1") == "1"


class PipelineError(RuntimeError):
    pass


def ensure_ref_indexed(ref_fasta: str) -> None:
    if not os.path.exists(ref_fasta):
        raise PipelineError(f"Reference FASTA not found: {ref_fasta}")
    if not os.path.exists(ref_fasta + ".fai"):
        _run(["samtools", "faidx", ref_fasta])
    aligner = _pick_aligner()
    marker = ref_fasta + (".bwt.2bit.64" if aligner == "bwa-mem2" else ".bwt")
    if not os.path.exists(marker):
        bt.logging.info(f"Building {aligner} index for {ref_fasta} (one-time)")
        _run([aligner, "index", ref_fasta])
    if not os.path.exists(ref_fasta.rsplit(".", 1)[0] + ".dict") and \
       not os.path.exists(ref_fasta + ".dict"):
        # samtools dict for tools that want a sequence dictionary
        dict_path = ref_fasta.rsplit(".", 1)[0] + ".dict"
        _run(["samtools", "dict", ref_fasta, "-o", dict_path])


def download_parallel(urls_and_dests: list[tuple[str, str]]) -> list[str]:
    """Download multiple files concurrently. Returns local paths in order."""
    def _fetch(url_dest):
        url, dest = url_dest
        if not url.startswith(("http://", "https://")):
            return url
        urllib.request.urlretrieve(url, dest)
        return dest

    with concurrent.futures.ThreadPoolExecutor(max_workers=len(urls_and_dests)) as pool:
        return list(pool.map(_fetch, urls_and_dests))


def align(ref_fasta: str, read1: str, read2: str, work_dir: str) -> str:
    bam_path = os.path.join(work_dir, "aligned.bam")
    aligner = _pick_aligner()
    threads = max(2, os.cpu_count() or 4)
    sort_threads = max(2, threads // 2)
    # -R adds a minimal read group (DeepVariant requires one)
    cmd = (
        f"{aligner} mem -t {threads} -R '@RG\\tID:niome\\tSM:miner\\tLB:lib1\\tPL:ILLUMINA' "
        f"{ref_fasta} {read1} {read2} "
        f"| samtools sort -@ {sort_threads} -o {bam_path} -"
    )
    _run(cmd, shell=True)
    _run(["samtools", "index", "-@", str(sort_threads), bam_path])
    return bam_path


def call_variants(ref_fasta: str, bam: str, region: str, work_dir: str) -> str:
    if CALLER == "deepvariant":
        return _call_deepvariant(ref_fasta, bam, region, work_dir)
    if CALLER == "clair3":
        return _call_clair3(ref_fasta, bam, region, work_dir)
    if CALLER == "bcftools":
        return _call_bcftools(ref_fasta, bam, region, work_dir)
    if CALLER == "dual":
        return _call_dual(ref_fasta, bam, region, work_dir)
    if CALLER == "strelka":
        return _call_strelka(ref_fasta, bam, region, work_dir)
    if CALLER == "triple":
        return _call_triple(ref_fasta, bam, region, work_dir)
    raise PipelineError(f"Unknown NIOME_CALLER={CALLER!r}")


def _call_bcftools_supplementary(ref_fasta: str, bam: str, region: str, work_dir: str) -> str:
    """bcftools call for indel sensitivity. Saves to separate filename to avoid conflict."""
    out_vcf = os.path.join(work_dir, "bcftools_calls.vcf.gz")
    cmd = (
        f"bcftools mpileup -f {ref_fasta} -r {region} --max-depth 1000 --min-BQ 5 --min-MQ 5 -a AD,DP,SP {bam} "
        f"| bcftools call -mv --prior 0.001 -Oz -o {out_vcf}"
    )
    _run(cmd, shell=True)
    _run(["bcftools", "index", "-f", out_vcf])
    return out_vcf


def _call_dual(ref_fasta: str, bam: str, region: str, work_dir: str) -> str:
    """Run DeepVariant + bcftools, merge calls."""
    dv_vcf = _call_deepvariant(ref_fasta, bam, region, work_dir)
    bcf_vcf = _call_bcftools_supplementary(ref_fasta, bam, region, work_dir)
    merged = os.path.join(work_dir, "merged_calls.vcf.gz")
    _run(
        f"bcftools concat -a {dv_vcf} {bcf_vcf} 2>/dev/null "
        f"| bcftools sort 2>/dev/null "
        f"| bcftools norm -d all -f {ref_fasta} -Oz -o {merged}",
        shell=True
    )
    _run(["bcftools", "index", "-f", merged])
    return merged


STRELKA_IMAGE = os.environ.get(
    "NIOME_STRELKA_IMAGE", "quay.io/biocontainers/strelka:2.9.10--0"
)


def _call_strelka(ref_fasta: str, bam: str, region: str, work_dir: str) -> str:
    """Strelka2 germline workflow. Industry-standard indel sensitivity.

    Strelka2 needs to:
      1. Configure the workflow with input BAM + reference + region
      2. Run the workflow (multi-threaded)
      3. The output VCF is at <run_dir>/results/variants/variants.vcf.gz
    """
    chrom, _, span = region.partition(":")
    start, _, end = span.partition("-")
    threads = max(1, (os.cpu_count() or 4) // 2)

    # Strelka writes to its own run-dir. Must NOT exist or it errors.
    run_dir = os.path.join(work_dir, "strelka_run")
    if os.path.exists(run_dir):
        shutil.rmtree(run_dir)

    # Write a BED file for the region (Strelka uses --callRegions)
    bed_path = os.path.join(work_dir, "strelka_region.bed")
    if start and end:
        with open(bed_path, "w") as f:
            f.write(f"{chrom}\t{start}\t{end}\n")
    else:
        # Whole chromosome — write full length from FASTA index
        bed_path = None

    ref_dir = os.path.dirname(os.path.abspath(ref_fasta))
    bam_dir = os.path.dirname(os.path.abspath(bam))
    out_dir = os.path.abspath(work_dir)
    bed_in_container = "/out/strelka_region.bed.gz" if bed_path else None

    # bgzip + tabix the BED for Strelka (--callRegions requires .bed.gz with .tbi)
    if bed_path:
        _run(["bgzip", "-f", bed_path])
        bgz_path = bed_path + ".gz"
        _run(["tabix", "-f", "-p", "bed", bgz_path])

    # Configure
    configure_cmd = [
        "docker", "run", "--rm",
        "-v", f"{ref_dir}:/ref",
        "-v", f"{bam_dir}:/bam",
        "-v", f"{out_dir}:/out",
        STRELKA_IMAGE,
        "configureStrelkaGermlineWorkflow.py",
        f"--bam=/bam/{os.path.basename(bam)}",
        f"--referenceFasta=/ref/{os.path.basename(ref_fasta)}",
        f"--runDir=/out/strelka_run",
    ]
    if bed_path:
        configure_cmd.append(f"--callRegions=/out/strelka_region.bed.gz")
    _run(configure_cmd)

    # Run the workflow
    run_cmd = [
        "docker", "run", "--rm",
        "-v", f"{ref_dir}:/ref",
        "-v", f"{bam_dir}:/bam",
        "-v", f"{out_dir}:/out",
        STRELKA_IMAGE,
        "/out/strelka_run/runWorkflow.py",
        "-m", "local",
        "-j", str(threads),
    ]
    _run(run_cmd)

    # Strelka output
    raw_vcf = os.path.join(run_dir, "results", "variants", "variants.vcf.gz")
    if not os.path.exists(raw_vcf):
        raise PipelineError(f"Strelka did not produce {raw_vcf}")

    # Copy to a stable location for downstream
    out_vcf = os.path.join(work_dir, "strelka_calls.vcf.gz")
    shutil.copy2(raw_vcf, out_vcf)
    _run(["bcftools", "index", "-f", out_vcf])
    return out_vcf


def _call_triple(ref_fasta: str, bam: str, region: str, work_dir: str) -> str:
    """DeepVariant + bcftools + Strelka2. Merge all three.

    Each catches different variant categories:
      - DeepVariant: high-VAF SNVs + clear indels (the workhorse)
      - bcftools: secondary check for over/under-calling
      - Strelka2: low-VAF indels (DeepVariant's weakness)

    Combined → ClinVar filter (in downstream annotate step) → only true CFTR variants survive.
    """
    dv_vcf = _call_deepvariant(ref_fasta, bam, region, work_dir)
    try:
        strelka_vcf = _call_strelka(ref_fasta, bam, region, work_dir)
    except PipelineError as e:
        bt.logging.warning(f"Strelka failed, continuing with DV+bcftools only: {e}")
        strelka_vcf = None
    bcf_vcf = _call_bcftools_supplementary(ref_fasta, bam, region, work_dir)

    inputs = [dv_vcf, bcf_vcf]
    if strelka_vcf:
        inputs.append(strelka_vcf)

    merged = os.path.join(work_dir, "merged_calls.vcf.gz")
    _run(
        f"bcftools concat -a {' '.join(inputs)} 2>/dev/null "
        f"| bcftools sort 2>/dev/null "
        f"| bcftools norm -d all -f {ref_fasta} -Oz -o {merged}",
        shell=True
    )
    _run(["bcftools", "index", "-f", merged])
    return merged


def annotate_with_dbsnp(vcf: str, work_dir: str) -> str:
    """Annotate VCF ID column with ClinVar IDs, then filter to ClinVar-annotated only."""
    if not DBSNP_VCF or not os.path.exists(DBSNP_VCF):
        return vcf
    annotated = os.path.join(work_dir, "rsid_annotated.vcf.gz")
    _run([
        "bcftools", "annotate",
        "-a", DBSNP_VCF,
        "-c", "ID",
        vcf, "-Oz", "-o", annotated,
    ])
    _run(["bcftools", "index", "-f", annotated])
    # Drop variants without ClinVar IDs - eliminates most FPs
    out = os.path.join(work_dir, "clinvar_only.vcf.gz")
    _run(f"bcftools view -e 'ID=\".\"' {annotated} -Oz -o {out}", shell=True)
    _run(["bcftools", "index", "-f", out])
    return out


def filter_and_normalize(vcf: str, ref_fasta: str, work_dir: str) -> str:
    """Apply QUAL filter, then bcftools-norm (left-align + split multi-allelics).

    Matches the validator's normalization exactly (scoring.py:71-83), so any
    variant that survives here will survive in the validator's pipeline too.
    """
    filtered = os.path.join(work_dir, "filtered.vcf.gz")
    if MIN_QUAL > 0:
        # -f 'PASS,.' keeps records with FILTER=PASS or FILTER=. (bcftools call
        # leaves FILTER unset; DeepVariant/Clair3 set it to PASS).
        _run(
            f"bcftools view -e 'QUAL<{MIN_QUAL}' -f 'PASS,.' {vcf} -Oz -o {filtered}",
            shell=True,
        )
        _run(["bcftools", "index", "-f", filtered])
        src = filtered
    else:
        src = vcf
    out = os.path.join(work_dir, "normalized.vcf.gz")
    _run([
        "bcftools", "norm",
        "-f", ref_fasta,
        "-c", "x",
        "-m", "-both",
        src, "-Oz", "-o", out,
    ])
    _run(["bcftools", "index", "-f", out])
    return out


def vcf_to_text(vcf_gz: str) -> str:
    return _run(["bcftools", "view", vcf_gz], capture=True)


def run_pipeline(
    read1_url: str,
    read2_url: str,
    chromosome: str,
    region: str,
    ref_fasta: Optional[str] = None,
    work_dir: Optional[str] = None,
    cleanup: bool = True,
) -> str:
    """Returns normalized VCF as plain text (ready for `synapse.vcf_content`)."""
    ref_fasta = ref_fasta or REF_FASTA
    if not ref_fasta:
        raise PipelineError("Set NIOME_REF_FASTA or pass ref_fasta=...")

    own_dir = work_dir is None
    work_dir = work_dir or tempfile.mkdtemp(prefix="niome_task_")
    timings = {}
    try:
        t0 = time.time()
        r1, r2 = download_parallel([
            (read1_url, os.path.join(work_dir, "r1.fq.gz")),
            (read2_url, os.path.join(work_dir, "r2.fq.gz")),
        ])
        timings["download"] = time.time() - t0

        t0 = time.time()
        bam = align(ref_fasta, r1, r2, work_dir)
        timings["align"] = time.time() - t0

        t0 = time.time()
        region_str = f"{chromosome}:{region}" if ":" not in region else region
        raw_vcf = call_variants(ref_fasta, bam, region_str, work_dir)
        timings["call"] = time.time() - t0

        t0 = time.time()
        raw_vcf = annotate_with_dbsnp(raw_vcf, work_dir)
        timings["annotate"] = time.time() - t0

        t0 = time.time()
        norm_vcf = filter_and_normalize(raw_vcf, ref_fasta, work_dir)
        text = vcf_to_text(norm_vcf)
        timings["normalize"] = time.time() - t0

        bt.logging.info(
            f"Pipeline timings (s): " +
            ", ".join(f"{k}={v:.2f}" for k, v in timings.items())
        )
        return text
    finally:
        if cleanup and own_dir:
            shutil.rmtree(work_dir, ignore_errors=True)


def _call_deepvariant(ref_fasta: str, bam: str, region: str, work_dir: str) -> str:
    out_vcf = os.path.join(work_dir, "calls.vcf.gz")
    ref_dir = os.path.dirname(os.path.abspath(ref_fasta))
    bam_dir = os.path.dirname(os.path.abspath(bam))
    out_dir = os.path.abspath(work_dir)
    shards = max(1, os.cpu_count() or 4)
    cmd = ["docker", "run", "--rm"]
    if USE_GPU:
        cmd += ["--gpus", DV_GPU_DEVICE]
    cmd += [
        "-v", f"{ref_dir}:/ref",
        "-v", f"{bam_dir}:/bam",
        "-v", f"{out_dir}:/out",
        DEEPVARIANT_IMAGE,
        "/opt/deepvariant/bin/run_deepvariant",
        f"--model_type={DV_MODEL}",
        f"--ref=/ref/{os.path.basename(ref_fasta)}",
        f"--reads=/bam/{os.path.basename(bam)}",
        f"--regions={region}",
        "--output_vcf=/out/calls.vcf.gz",
        f"--num_shards={shards}",
        "--intermediate_results_dir=/out/intermediate",
    ]
    _run(cmd)
    return out_vcf


def _call_clair3(ref_fasta: str, bam: str, region: str, work_dir: str) -> str:
    clair3_bin = "/home/po/miniconda3/envs/clair3/bin"
    if clair3_bin not in os.environ.get("PATH", ""):
        os.environ["PATH"] = clair3_bin + ":" + os.environ.get("PATH", "")
    if not CLAIR3_MODEL_PATH:
        raise PipelineError("NIOME_CLAIR3_MODEL_PATH not set")
    out_dir = os.path.join(work_dir, "clair3_out")
    os.makedirs(out_dir, exist_ok=True)
    chrom, _, span = region.partition(":")
    start, _, end = span.partition("-")
    threads = max(1, os.cpu_count() or 4)
    cmd = [
        "/home/po/miniconda3/envs/clair3/bin/run_clair3.sh",
        f"--bam_fn={bam}",
        f"--ref_fn={ref_fasta}",
        f"--threads={threads}",
        f"--platform={CLAIR3_PLATFORM}",
        f"--model_path={CLAIR3_MODEL_PATH}",
        f"--output={out_dir}",
        f"--ctg_name={chrom}",
    ]
    if start and end:
        bed_path = os.path.join(work_dir, "region.bed")
        with open(bed_path, "w") as bf:
            bf.write(f"{chrom}\t{start}\t{end}\n")
        cmd.append(f"--bed_fn={bed_path}")
    cmd.extend([
        "--pypy=/home/po/miniconda3/envs/clair3/bin/pypy3",
        "--python=/home/po/miniconda3/envs/clair3/bin/python",
        "--samtools=/home/po/miniconda3/envs/clair3/bin/samtools",
    ])
    _run(cmd)
    merged = os.path.join(out_dir, "merge_output.vcf.gz")
    if not os.path.exists(merged):
        raise PipelineError(f"Clair3 did not produce {merged}")
    return merged


def _call_bcftools(ref_fasta: str, bam: str, region: str, work_dir: str) -> str:
    out_vcf = os.path.join(work_dir, "calls.vcf.gz")
    cmd = (
        f"bcftools mpileup -f {ref_fasta} -r {region} --max-depth 1000 --min-BQ 5 --min-MQ 5 -a AD,DP,SP {bam} "
        f"| bcftools call -mv --prior 0.001 -Oz -o {out_vcf}"
    )
    _run(cmd, shell=True)
    _run(["bcftools", "index", "-f", out_vcf])
    return out_vcf


def _pick_aligner() -> str:
    return "bwa-mem2" if shutil.which("bwa-mem2") else "bwa"


def _run(cmd, shell: bool = False, capture: bool = False) -> str:
    result = subprocess.run(
        cmd, shell=shell, check=False,
        stdout=subprocess.PIPE if capture else subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    if result.returncode != 0:
        snippet = (result.stderr or "")[-500:]
        raise PipelineError(
            f"Command failed (exit {result.returncode}): {cmd}\nstderr tail: {snippet}"
        )
    return result.stdout if capture else ""
