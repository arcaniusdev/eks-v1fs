#!/usr/bin/env python3
"""
Safely upgrade both V1FS Helm releases (my-release and rv)
while preserving all custom values required by the EKS scanning pipeline.

Usage:
    python3 upgrade.py [--version VERSION] [--dry-run] [--skip-sanity]

Run from the bastion host via SSM. Requires helm, kubectl, and aws CLI.
"""
import argparse
import json
import subprocess
import sys
import time

# Custom Helm values that MUST be specified on every upgrade.
# A plain `helm upgrade` without these reverts to chart defaults,
# re-enabling HPA (conflicts with KEDA) and resetting resources.
CUSTOM_VALUES = {
    "scanner.autoscaling.enabled": "false",
    "scanner.resources.requests.cpu": "800m",
    "scanner.resources.requests.memory": "2Gi",
    "visiononeFilesecurity.management.dbEnabled": "true",
    "databaseContainer.storageClass.create": "false",
    "databaseContainer.persistence.storageClassName": "gp3",
    "databaseContainer.persistence.size": "100Gi",
    "scanner.ephemeralVolume.enabled": "true",
    "scanner.ephemeralVolume.storageClass": "efs-sc",
    "scanner.ephemeralVolume.accessMode": "ReadWriteMany",
    "scanner.ephemeralVolume.size": "100Gi",
}

# CLISH scan policy field names mapped to their modify flag names.
CLISH_FIELD_MAP = {
    "Max Decompress Layer Limit": "max-decompression-layer",
    "Max Decompress Ratio Limit": "max-decompression-ratio",
    "Max Decompression File Count": "max-decompression-file-count",
    "Max Decompression Size": "max-decompression-size",
}

NAMESPACE = "visionone-filesecurity"
REVIEW_NAMESPACE = "visionone-review"
RELEASES = [
    ("my-release", NAMESPACE),
    ("rv", REVIEW_NAMESPACE),
]
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
                if val:
                    policy[flag_name] = val
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


def build_upgrade_cmd(release, namespace, version=None):
    """Build the helm upgrade command with all custom values."""
    cmd = f"helm upgrade {release} visionone-filesecurity/visionone-filesecurity -n {namespace}"
    if version:
        cmd += f" --version {version}"
    for key, val in CUSTOM_VALUES.items():
        cmd += f" --set {key}={val}"
    return cmd


def main():
    parser = argparse.ArgumentParser(description="Upgrade V1FS Helm releases safely")
    parser.add_argument("--version", help="Chart version to upgrade to (default: latest)")
    parser.add_argument("--dry-run", action="store_true", help="Show commands without executing")
    parser.add_argument("--skip-sanity", action="store_true", help="Skip the sanity scan check")
    args = parser.parse_args()

    print("=" * 70)
    print("V1FS Scanner Upgrade — Safe Upgrade with Custom Values")
    print("=" * 70)

    # Step 0: Ensure environment is set up
    print("\n[0/8] Setting up environment...")
    import os
    if not os.environ.get("KUBECONFIG"):
        os.environ["KUBECONFIG"] = "/root/.kube/config"
        print("  Set KUBECONFIG=/root/.kube/config")
    # Ensure helm repo is added (idempotent)
    run(
        "helm repo add visionone-filesecurity "
        "https://trendmicro.github.io/visionone-file-security-helm/ 2>/dev/null || true",
        check=False,
    )

    # Step 1: Check current versions
    print("\n[1/8] Checking current versions...")
    for release, ns in RELEASES:
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

    # Step 4: Upgrade both releases
    for release, ns in RELEASES:
        print(f"\n[4/8] Upgrading {release} in {ns}...")
        cmd = build_upgrade_cmd(release, ns, args.version)
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

    # Step 6: Verify no HPA conflict
    print("\n[6/8] Checking for HPA conflicts...")
    result = run(f"kubectl get hpa -n {NAMESPACE} --no-headers 2>&1", check=False)
    if result.stdout.strip() and "No resources found" not in result.stdout:
        print("  WARNING: HPA detected! This conflicts with KEDA. Deleting...")
        if not args.dry_run:
            run(f"kubectl delete hpa -n {NAMESPACE} --all")
    else:
        print("  OK — no HPA found.")

    # Step 7: Verify ScaledObjects and pods
    print("\n[7/8] Verifying infrastructure...")
    run(f"kubectl get scaledobject -n {NAMESPACE}")
    run(f"kubectl get pods -n {NAMESPACE}")
    print("\n  Scanner pod resources:")
    run(
        f"kubectl describe pod -n {NAMESPACE} -l app.kubernetes.io/component=scanner 2>&1 "
        f"| grep -A 3 'Requests:' | head -20"
    )

    # Step 8: Sanity scan
    if args.skip_sanity:
        print("\n[8/8] Skipping sanity scan (--skip-sanity)")
    else:
        print("\n[8/8] Sanity scan...")
        if args.dry_run:
            print("  DRY RUN: Would upload clean + EICAR test files")
        else:
            # Get ingest bucket from scanner-app configmap
            result = run(
                f"kubectl get configmap scanner-app-config -n {NAMESPACE} "
                f"-o jsonpath='{{.data.S3_INGEST_BUCKET}}'",
            )
            ingest = result.stdout.strip().strip("'")
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
                print("  WARNING: Could not determine ingest bucket. Skipping sanity scan.")

    # Summary
    print("\n" + "=" * 70)
    print("Upgrade complete.")
    for release, ns in RELEASES:
        chart, app = get_installed_version(release, ns)
        print(f"  {release} ({ns}): chart={chart}, app_version={app}")
    print("=" * 70)


if __name__ == "__main__":
    main()
