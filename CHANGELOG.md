# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Each release tag is what the CloudFormation template's `RepoRef` parameter
defaults to, so a template downloaded at tag `vX.Y.Z` deploys the matching
scripts, app code, and Helm values.

## [Unreleased]

## [2.2.0]
### Added
- **Unified scanning app in two language flavors.** One app that does everything
  (drain SQS, scan, full routing, audit, reconciliation), available as **Python**
  (`app/`) or **Java** (`app-java/`), selected by the `ScannerAppFlavor` parameter.
  Both read the same environment contract.
- **Env-toggled dispatch** (`ScannerDispatchMode`): `clusterip` (one gRPC
  connection to the in-cluster Service) or `pull` (discover live scanner pod IPs
  from the NLB target group and dispatch each scan to the least-busy pod
  directly — no load balancer in the scan path). Pull mode adds a `PullDispatch`
  condition, an NLB-required rule, ELB pod-discovery IAM, and bastion
  target-group discovery.
### Changed
- The three `reference/` options (python-default, python-KEDA, java-KEDA) are now
  scenarios that configure this one app (flavor + dispatch + scaling) rather than
  three separate programs; POC guides rewritten to match.
- Java image builds its Maven stage natively via `$BUILDPLATFORM` (arch-independent
  jar), so cross-building for ARM nodes no longer emulates the whole build.

## [2.1.1]
### Fixed
- External-queue mode: an empty `S3_INGEST_BUCKET` no longer trips `deploy.sh`'s
  stack-Outputs fallback (which failed during `CREATE_IN_PROGRESS`). Optional
  buckets are bound under `set -u`; `get_output` hardened against null Outputs.

## [2.1.0]
### Added
- **Tag-in-place routing**: clean files are tagged `S3-Clean` in the ingest bucket
  (never moved); malicious files move to quarantine; decompression-limit files get
  a distinct `S3-DecompressionLimit` + `ScanErrors` tag.
- **External-queue drain mode**: `ExternalScanQueueArn` + `ExternalScanSourceBucketArns`
  let the scanner-app drain a user-owned SQS queue instead of a stack-built one.
- Documented the KEDA-mode safe Helm-upgrade path in the README and both KEDA POC guides.
### Changed
- **Graviton ARM (`r8g.xlarge`) is the default node type.**
### Removed
- The dedicated "clean bucket" (clean files are now tagged in place).

## [2.0.0]
### Added
- **Three deployment options** from one template via `ScannerScalingMode`
  (`hpa` | `keda`): `reference/python-default` (chart-HPA, TrendAI-supported),
  `reference/python-KEDA` and `reference/java-KEDA` (KEDA queue-depth scaling +
  client-side pull/semaphore dispatcher), each with its own POC guide.

## [1.0.2]
### Added
- **Graviton (arm64) node support** (`r8g`/`r7g`), with automatic AMI selection
  and cross-arch scanner-app image build.
### Fixed
- Missing `import re` in `scanner.py` (present in the broken `1.0.1`).

## [1.0.1]
Broken — missing `import re` crashes the scanner-app. Do not deploy; use `1.0.2`.

## [1.0.0]
### Added
- Initial evaluation-friendly EKS deployment of the TrendAI Vision One File
  Security containerized scanner, aligned with TrendAI's supported methodology
  (chart-native HPA, Cluster Autoscaler, pinned chart). Single CloudFormation
  template with parameter-toggled modules (scanner-app, review pipeline,
  existing-bucket, endpoint exposure).
