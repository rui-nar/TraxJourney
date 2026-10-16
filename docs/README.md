# Documentation

Reference docs describe how the system works today and are kept current with
the code. Plans and review ledgers are the record of how it got there.

## Operating the service

| Doc | What it covers |
|---|---|
| [DEPLOYMENT_VPS](DEPLOYMENT_VPS.md) | The production and validation stacks on the VPS: install, deploy, webhook, rail data |
| [RELEASING](RELEASING.md) | Version bumps, tags, release notes, post-deploy owner actions |
| [LOGGING](LOGGING.md) | Log format, levels and privacy rules |
| [METRICS](METRICS.md) | Prometheus metrics and the dashboards that read them |
| [OBSERVABILITY](OBSERVABILITY.md) | The Loki / Prometheus / Grafana stack |
| [ADMIN](ADMIN.md) | The admin dashboard |
| [BILLING](BILLING.md) | Plans, Stripe, refunds and withdrawal |

## Clients and stores

| Doc | What it covers |
|---|---|
| [ANDROID](ANDROID.md) | Android build, signing and Play |
| [IOS](IOS.md) | iOS build, signing and TestFlight without a Mac |
| [LAUNCH_QA](LAUNCH_QA.md) | v1.0 launch checks per platform |

## Features

| Doc | What it covers |
|---|---|
| [ENCRYPTION](ENCRYPTION.md) | End-to-end encryption: tiers, keys, what is encrypted |
| [ENCOUNTERS](ENCOUNTERS.md) | Encounters on the map |
| [VIDEO](VIDEO.md) | Trip video rendering, its stages and benchmarks |

## How we work

| Doc | What it covers |
|---|---|
| [REVIEW](REVIEW.md) | Adversarial review policy; ledgers in [`reviews/`](reviews/) |
| [DELIVERY](DELIVERY.md) | Delivering a plan with implementer agents |
| [`plans/`](plans/) | Implementation plans and what delivered them |
| [`repro/`](repro/) | Reproductions too slow or heavy for `tests/` |
