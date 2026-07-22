#!/usr/bin/env python3
"""
Safely upgrade the V1FS Helm release(s) while preserving installed values.

The main release (my-release) is always upgraded; the review release (rv)
is upgraded only if it is installed. Install-time values are preserved by
capturing `helm get values` for each release and re-applying them, layered
on top of helm/values-base.yaml (the repo's single source of truth). Live
HPA min/max replicas are read from the cluster so operator tuning survives
the upgrade.

Usage:
    python3 upgrade.py [--version VERSION] [--dry-run] [--skip-sanity]

Run from the bastion host via SSM. Requires helm, kubectl, and aws CLI.
"""
import argparse
import json
import os
import subprocess
import sys
import sys
import time

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
VALUES_BASE = os.path.join(SCRIPT_DIR, "..", "helm", "values-base.yaml")

# CLISH scan policy field names mapped to their modify flag names.
CLISH_FIELD_MAP = {
    "Max Decompress Layer Limit": "max-decompression-layer",
    "Max Decompress Ratio Limit": "max-decompression-ratio",
    "Max Decompression File Count": "max-decompression-file-count",
    "Max Decompression Size": "max-decompression-size",
}

NAMESPACE = "visionone-filesecurity"
REVIEW_NAMESPACE = "visionone-review"
MGMT_DEPLOY = "my-release-visionone-filesecurity-management-service"


def run(cmd, check=True, capture=True):
    """Run a shell command and return output."""
    print(f"  $ {cmd}")
    result = subprocess.run(cmd, shell=True, capture_output=capture, text=True)
    if capture and result.stdout:
        print(result.stdout.rstrip())
    if capture and result.stderr:
        print(result.stderr.rstrip(), file=sys.stderr)
    if check and result.returncode != 0:
        print(f"ERROR: Command failed with exit code {result.returncode}")
        sys.exit(1)
    return result


def discover_releases():
    """Return [(release, namespace)] — my-release always, rv only if installed."""
    releases = [("my-release", NAMESPACE)]
    result = run(f"helm list -n {REVIEW_NAMESPACE} -f '^rv$' -o json", check=False)
    if result.returncode == 0 and result.stdout.strip():
        if json.loads(result.stdout):
            releases.append(("rv", REVIEW_NAMESPACE))
    return releases


def get_current_scan_policy():
    """Query the current CLISH scan policy values from the running management service."""
    result = run(
        f"kubectl exec deploy/{MGMT_DEPLOY} -n {NAMESPACE} -- "
        f"clish scanner scan-policy show",
        check=False,
    )
    policy = {}
    if result.returncode != 0:
        print("  WARNING: Could not query current scan policy. Using no policy values.")
        return policy
    for line in result.stdout.splitlines():
        line = line.strip()
        for field_label, flag_name in CLISH_FIELD_MAP.items():
            if line.startswith(field_label):
                # Parse value from "Max Decompress Layer Limit : 10" or "Max Decompression Size : 512 MB"
                val = line.split(":")[-1].strip().replace(" MB", "")
                # These are always integers. Validate before use — the values
                # are later interpolated into a shell command, so rejecting
                # non-numeric input closes any command-injection surface if the
                # CLISH output format ever changes unexpectedly.
                if val.isdigit():
                    policy[flag_name] = val
                elif val:
                    print(f"  WARNING: non-numeric scan-policy value for {flag_name!r}: {val!r} — skipping")
    return policy


def get_installed_version(release, namespace):
    """Get the currently installed chart version for a release."""
    result = run(
        f"helm list -n {namespace} -f '^{release}$' -o json",
        check=False,
    )
    if result.returncode == 0 and result.stdout.strip():
        data = json.loads(result.stdout)
        if data:
            return data[0].get("chart", "unknown"), data[0].get("app_version", "unknown")
    return "unknown", "unknown"


def capture_release_values(release, namespace):
    """Save the release's user-supplied values to a temp file; return its path."""
    path = f"/tmp/upgrade-values-{release}.yaml"
    result = run(f"helm get values {release} -n {namespace} -o yaml", check=False)
    content = result.stdout if result.returncode == 0 else ""
    if content.strip() in ("", "null"):
        content = "{}\n"
    with open(path, "w") as f:
        f.write(content)
    print(f"  Captured install-time values → {path}")
    return path


SCANNER_SCALEDOBJECT = "v1fs-scanner-sqs-scaler"


def scanner_keda_present(namespace):
    """True if the KEDA ScaledObject scales the V1FS scanner (this branch's mode)."""
    r = run(f"kubectl get scaledobject {SCANNER_SCALEDOBJECT} -n {namespace} "
            f"--ignore-not-found -o name", check=False)
    return r.returncode == 0 and bool(r.stdout.strip())


def chart_hpa_on_scanner(namespace):
    """Return (min,max) of a CHART-owned (non-KEDA) HPA on the scanner, else None.

    KEDA creates its own HPA for the ScaledObject; that one carries the
    'scaledobject.keda.sh/name' label. A chart HPA does NOT — its presence
    alongside the ScaledObject means two autoscalers are fighting the scanner.
    """
    result = run(f"kubectl get hpa -n {namespace} -o json", check=False)
    if result.returncode != 0 or not result.stdout.strip():
        return None
    try:
        items = json.loads(result.stdout).get("items", [])
    except json.JSONDecodeError:
        return None
    for hpa in items:
        target = hpa.get("spec", {}).get("scaleTargetRef", {}).get("name", "")
        labels = hpa.get("metadata", {}).get("labels", {}) or {}
        if "visionone-filesecurity-scanner" in target and "scaledobject.keda.sh/name" not in labels:
            spec = hpa["spec"]
            return spec.get("minReplicas", 1), spec.get("maxReplicas")
    return None


def build_upgrade_cmd(release, namespace, values_file, hpa_bounds, keda_scanner, version=None):
    """Build the helm upgrade command: base values + preserved install values.

    KEDA-scanner mode (this branch's default): the chart HPA stays disabled
    (values-base scanner.autoscaling.enabled=false) and the KEDA ScaledObject —
    untouched by helm — owns scaling, so pass NO autoscaling bounds. BYO/chart-
    HPA fallback: preserve the live HPA bounds as before.
    """
    cmd = (
        f"helm upgrade {release} visionone-filesecurity/visionone-filesecurity "
        f"-n {namespace} -f {VALUES_BASE} -f {values_file}"
    )
    if version:
        cmd += f" --version {version}"
    if keda_scanner:
        # Belt-and-suspenders: re-assert the chart HPA stays off so a chart
        # default flip can't resurrect it to fight KEDA.
        cmd += " --set scanner.autoscaling.enabled=false"
    elif hpa_bounds:
        min_r, max_r = hpa_bounds
        if min_r:
            cmd += f" --set scanner.autoscaling.minReplicas={min_r}"
        if max_r:
            cmd += f" --set scanner.autoscaling.maxReplicas={max_r}"
    return cmd


def main():
    parser = argparse.ArgumentParser(description="Upgrade V1FS Helm releases safely")
    parser.add_argument("--version", help="Chart version to upgrade to (default: latest)")
    parser.add_argument("--dry-run", action="store_true", help="Show commands without executing")
    parser.add_argument("--skip-sanity", action="store_true", help="Skip the sanity scan check")
    args = parser.parse_args()

    print("=" * 70)
    print("V1FS Scanner Upgrade — Safe Upgrade with Preserved Values")
    print("=" * 70)

    # Step 0: Ensure environment is set up
    print("\n[0/8] Setting up environment...")
    if not os.environ.get("KUBECONFIG"):
        os.environ["KUBECONFIG"] = "/root/.kube/config"
        print("  Set KUBECONFIG=/root/.kube/config")
    if not os.path.isfile(VALUES_BASE):
        print(f"ERROR: {VALUES_BASE} not found — run from the repo checkout.")
        sys.exit(1)
    # Ensure helm repo is added (idempotent)
    run(
        "helm repo add visionone-filesecurity "
        "https://trendmicro.github.io/visionone-file-security-helm/ 2>/dev/null || true",
        check=False,
    )

    releases = discover_releases()
    print(f"  Releases to upgrade: {', '.join(r for r, _ in releases)}")

    # Step 1: Check current versions
    print("\n[1/8] Checking current versions...")
    for release, ns in releases:
        chart, app = get_installed_version(release, ns)
        print(f"  {release} ({ns}): chart={chart}, app_version={app}")

    # Step 2: Capture current scan policy before upgrade
    print("\n[2/8] Capturing current CLISH scan policy...")
    current_policy = get_current_scan_policy()
    if current_policy:
        for k, v in current_policy.items():
            print(f"  {k} = {v}")
    else:
        print("  No scan policy values found (will skip re-application).")

    # Step 3: Update repo and check for new version
    print("\n[3/8] Updating Helm repository...")
    run("helm repo update visionone-filesecurity")
    print("\nAvailable versions:")
    run("helm search repo visionone-filesecurity/visionone-filesecurity --versions | head -5")

    # Check if upgrade is needed
    if not args.version:
        latest_result = run(
            "helm search repo visionone-filesecurity/visionone-filesecurity -o json",
            check=False,
        )
        if latest_result.returncode == 0 and latest_result.stdout.strip():
            latest_data = json.loads(latest_result.stdout)
            if latest_data:
                latest_chart = latest_data[0].get("version", "")
                installed_chart, _ = get_installed_version("my-release", NAMESPACE)
                installed_ver = installed_chart.replace("visionone-filesecurity-", "")
                if installed_ver == latest_chart:
                    print(f"\n  Already running the latest version ({latest_chart}). Nothing to upgrade.")
                    print("  Use --version X.Y.Z to force a specific version.")
                    return

    # Step 4: Upgrade releases, preserving each release's installed values
    for release, ns in releases:
        print(f"\n[4/8] Upgrading {release} in {ns}...")
        values_file = capture_release_values(release, ns)
        keda_scanner = scanner_keda_present(ns)
        hpa_bounds = None
        if keda_scanner:
            print("  Scanner scaling: KEDA on queue depth (chart HPA disabled). "
                  "Bounds live on the ScaledObject; re-asserting autoscaling.enabled=false.")
        else:
            hpa_bounds = chart_hpa_on_scanner(ns)
            if hpa_bounds:
                print(f"  Scanner scaling: chart HPA (bounds min={hpa_bounds[0]}, max={hpa_bounds[1]}).")
            else:
                print("  WARNING: scanner has neither a KEDA ScaledObject nor a chart HPA — "
                      "it will not autoscale. Check the deployment mode.")
        cmd = build_upgrade_cmd(release, ns, values_file, hpa_bounds, keda_scanner, args.version)
        if args.dry_run:
            print(f"  DRY RUN: {cmd}")
        else:
            run(cmd)
            print(f"  {release} upgraded successfully.")

    # Step 5: Re-apply captured CLISH scan policy to my-release only
    print("\n[5/8] Re-applying CLISH scan policy to my-release...")
    if not current_policy:
        print("  SKIPPED — no scan policy was set before upgrade.")
    else:
        policy_args = " ".join(f"--{k}={v}" for k, v in current_policy.items())
        clish_cmd = (
            f"kubectl exec deploy/{MGMT_DEPLOY} -n {NAMESPACE} -- "
            f"clish scanner scan-policy modify {policy_args}"
        )
        if args.dry_run:
            print(f"  DRY RUN: {clish_cmd}")
        else:
            print("  Waiting for management service rollout...")
            run(f"kubectl rollout status deploy/{MGMT_DEPLOY} -n {NAMESPACE} --timeout=180s")
            run(clish_cmd)
            print("\n  Verifying scan policy:")
            run(
                f"kubectl exec deploy/{MGMT_DEPLOY} -n {NAMESPACE} -- "
                f"clish scanner scan-policy show"
            )
    print("  NOTE: rv intentionally has NO scan policy (unlimited decompression).")

    # Step 6: Verify the scanner has EXACTLY ONE autoscaler. This branch scales
    # the scanner with KEDA on queue depth, so the chart HPA MUST be absent; a
    # chart HPA coexisting with the KEDA ScaledObject means two controllers are
    # fighting the replica count — a hard failure (a chart default/schema flip
    # slipped past the values override). (BYO fallback: chart HPA, no KEDA.)
    print("\n[6/8] Verifying scanner autoscaling state...")
    conflict = False
    for release, ns in releases:
        keda = scanner_keda_present(ns)
        chart = chart_hpa_on_scanner(ns) is not None
        if keda and chart:
            print(f"  ERROR [{ns}]: BOTH a KEDA ScaledObject AND a chart HPA scale the "
                  f"scanner — they will thrash the replica count. Ensure "
                  f"scanner.autoscaling.enabled=false and delete the chart HPA "
                  f"(check: helm get values {release} -n {ns}).")
            conflict = True
        elif keda:
            print(f"  OK [{ns}] — scanner scaled by KEDA on queue depth; no chart HPA.")
        elif chart:
            print(f"  OK [{ns}] — scanner scaled by the chart HPA (BYO/CPU fallback).")
        else:
            print(f"  WARNING [{ns}]: scanner has no autoscaler at all — it will not scale.")
    if conflict:
        print("\nUPGRADE GUARD FAILED: chart HPA + KEDA both target the scanner. "
              "Reconcile before relying on this deployment.")
        sys.exit(1)

    # Step 7: Verify pods
    print("\n[7/8] Verifying infrastructure...")
    for _, ns in releases:
        run(f"kubectl get pods -n {ns}")

    # Step 8: Sanity scan
    if args.skip_sanity:
        print("\n[8/8] Skipping sanity scan (--skip-sanity)")
    else:
        print("\n[8/8] Sanity scan...")
        if args.dry_run:
            print("  DRY RUN: Would upload clean + EICAR test files")
        else:
            # Get ingest bucket from scanner-app configmap (absent when the
            # scanner-app module is not deployed)
            result = run(
                f"kubectl get configmap scanner-app-config -n {NAMESPACE} "
                f"-o jsonpath='{{.data.S3_INGEST_BUCKET}}' 2>/dev/null",
                check=False,
            )
            ingest = result.stdout.strip().strip("'") if result.returncode == 0 else ""
            if ingest:
                print(f"  Ingest bucket: {ingest}")
                run(f"echo 'upgrade-sanity-clean' | aws s3 cp - s3://{ingest}/upgrade-sanity-clean.txt")
                run(
                    f"printf 'X5O!P%%@AP[4\\\\PZX54(P^)7CC)7}}$EICAR-STANDARD-ANTIVIRUS-TEST-FILE!$H+H*' "
                    f"| aws s3 cp - s3://{ingest}/upgrade-sanity-eicar.txt"
                )
                print("  Waiting 30s for scan processing...")
                time.sleep(30)
                run(
                    f"kubectl logs -l app=scanner-app -n {NAMESPACE} --tail=10 2>&1 "
                    f"| grep -E 'upgrade-sanity'"
                )
            else:
                # Scanner-app module not deployed — point at the external endpoint instead
                print("  scanner-app not deployed — checking published scanner endpoint...")
                stack = os.environ.get("CFN_STACK_NAME", "")
                ep = ""
                if stack:
                    result = run(
                        f"aws ssm get-parameter --name /{stack}/scanner-endpoint "
                        f"--query Parameter.Value --output text 2>/dev/null",
                        check=False,
                    )
                    if result.returncode == 0:
                        ep = result.stdout.strip()
                if ep:
                    print(f"  Scanner endpoint: {ep}")
                    print("  Manual sanity scan (from a host with the V1FS SDK):")
                    print(f"    python3 -c \"import amaas.grpc; h=amaas.grpc.init('{ep}', API_KEY, False); ...\"")
                else:
                    print("  No scanner-app and no published endpoint found — skipping sanity scan.")

    # Summary
    print("\n" + "=" * 70)
    print("Upgrade complete.")
    for release, ns in releases:
        chart, app = get_installed_version(release, ns)
        print(f"  {release} ({ns}): chart={chart}, app_version={app}")
    print("=" * 70)


if __name__ == "__main__":
    main()
