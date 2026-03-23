#!/usr/bin/env python3
"""
Safely upgrade both V1FS Helm releases (my-release and review-release)
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

# CLISH scan policy for my-release only (NOT review-release).
SCAN_POLICY = {
    "max-decompression-layer": "10",
    "max-decompression-file-count": "1000",
    "max-decompression-ratio": "150",
    "max-decompression-size": "512",
}

NAMESPACE = "visionone-filesecurity"
RELEASES = ["my-release", "review-release"]
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


def get_installed_version(release):
    """Get the currently installed chart version for a release."""
    result = run(
        f"helm list -n {NAMESPACE} -f '^{release}$' -o json",
        check=False,
    )
    if result.returncode == 0 and result.stdout.strip():
        data = json.loads(result.stdout)
        if data:
            return data[0].get("chart", "unknown"), data[0].get("app_version", "unknown")
    return "unknown", "unknown"


def build_upgrade_cmd(release, version=None):
    """Build the helm upgrade command with all custom values."""
    cmd = f"helm upgrade {release} visionone-filesecurity/visionone-filesecurity -n {NAMESPACE}"
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

    # Step 1: Check current versions
    print("\n[1/7] Checking current versions...")
    for release in RELEASES:
        chart, app = get_installed_version(release)
        print(f"  {release}: chart={chart}, app_version={app}")

    # Step 2: Update repo and check available versions
    print("\n[2/7] Updating Helm repository...")
    run("helm repo update visionone-filesecurity")
    print("\nAvailable versions:")
    run("helm search repo visionone-filesecurity/visionone-filesecurity --versions | head -5")

    # Step 3: Upgrade both releases
    for release in RELEASES:
        print(f"\n[3/7] Upgrading {release}...")
        cmd = build_upgrade_cmd(release, args.version)
        if args.dry_run:
            print(f"  DRY RUN: {cmd}")
        else:
            run(cmd)
            print(f"  {release} upgraded successfully.")

    # Step 4: Re-apply CLISH scan policy to my-release only
    print("\n[4/7] Re-applying CLISH scan policy to my-release...")
    policy_args = " ".join(f"--{k}={v}" for k, v in SCAN_POLICY.items())
    clish_cmd = (
        f"kubectl exec deploy/{MGMT_DEPLOY} -n {NAMESPACE} -- "
        f"clish scanner scan-policy modify {policy_args}"
    )
    if args.dry_run:
        print(f"  DRY RUN: {clish_cmd}")
    else:
        # Wait for management service to be ready after upgrade
        print("  Waiting for management service rollout...")
        run(f"kubectl rollout status deploy/{MGMT_DEPLOY} -n {NAMESPACE} --timeout=180s")
        run(clish_cmd)
        print("\n  Verifying scan policy:")
        run(
            f"kubectl exec deploy/{MGMT_DEPLOY} -n {NAMESPACE} -- "
            f"clish scanner scan-policy show"
        )
    print("  NOTE: review-release intentionally has NO scan policy (unlimited decompression).")

    # Step 5: Verify no HPA conflict
    print("\n[5/7] Checking for HPA conflicts...")
    result = run(f"kubectl get hpa -n {NAMESPACE} --no-headers 2>&1", check=False)
    if result.stdout.strip() and "No resources found" not in result.stdout:
        print("  WARNING: HPA detected! This conflicts with KEDA. Deleting...")
        if not args.dry_run:
            run(f"kubectl delete hpa -n {NAMESPACE} --all")
    else:
        print("  OK — no HPA found.")

    # Step 6: Verify ScaledObjects and pods
    print("\n[6/7] Verifying infrastructure...")
    run(f"kubectl get scaledobject -n {NAMESPACE}")
    run(f"kubectl get pods -n {NAMESPACE}")
    print("\n  Scanner pod resources:")
    run(
        f"kubectl describe pod -n {NAMESPACE} -l app.kubernetes.io/component=scanner 2>&1 "
        f"| grep -A 3 'Requests:' | head -20"
    )

    # Step 7: Sanity scan
    if args.skip_sanity:
        print("\n[7/7] Skipping sanity scan (--skip-sanity)")
    else:
        print("\n[7/7] Sanity scan...")
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
    for release in RELEASES:
        chart, app = get_installed_version(release)
        print(f"  {release}: chart={chart}, app_version={app}")
    print("=" * 70)


if __name__ == "__main__":
    main()
