IDRIS ACHTERBERG
Senior Backend Engineer
Leeds, United Kingdom · idris.achterberg@example.com · 07700 900417 · github.example/idris-a

PROFILE

Backend engineer with nine years of experience building and operating Python services for logistics and payments. Most at home designing PostgreSQL schemas, making slow endpoints fast, and turning an on-call rota from a source of dread into something boring. Comfortable leading small projects end to end, from the first design document to the post-incident review. Looking for a team where reliability work and product work are treated as the same job.

EXPERIENCE

Senior Backend Engineer — Brambleway Logistics, Leeds (hybrid)
March 2019 – present

Brambleway runs route-planning and proof-of-delivery software for about 400 regional courier firms.

- Led the redesign of the dispatch service from a single Django monolith into three Python services (FastAPI, Celery, PostgreSQL), cutting the p95 latency of the route-assignment endpoint from 2.4 s to 310 ms.
- Designed the event pipeline that ingests roughly 1.2 million driver location pings a day through Redis Streams into partitioned PostgreSQL tables; this retired a third-party tracking vendor and saves about £90,000 a year.
- Introduced idempotency keys and an outbox table for delivery-status webhooks after an incident in which customers were notified twice; duplicate notifications dropped to zero over the following six months.
- Owned the migration from self-managed PostgreSQL 11 on EC2 to Amazon RDS for PostgreSQL 15, with under four minutes of planned downtime.
- Wrote and maintain the team's service template (structured logging, OpenTelemetry tracing, health checks), now used by eleven services.
- Mentor two mid-level engineers and run the fortnightly architecture review.
- Take part in the on-call rota (one week in six) and wrote nine of the team's twenty-three runbooks.

Backend Engineer — Corvid Payments, Manchester
June 2016 – February 2019

Corvid provided card-payment reconciliation for independent retailers.

- Built the nightly settlement reconciliation job in Python, matching about 80,000 card transactions a night against acquirer files and replacing a spreadsheet process run by two people.
- Wrote a small internal service in Go to parse and validate acquirer settlement files. It was my first Go in production and the only Go service the team ran.
- Added property-based tests (Hypothesis) to the fee-calculation module and found two rounding defects that had been under-charging merchants.
- Helped prepare evidence for the company's PCI DSS assessment: access reviews, log retention and change-management records.

Junior Developer — Saltmarsh Digital, Leeds
September 2014 – May 2016

A twelve-person web agency.

- Built and maintained Django and WordPress sites for charities and local councils.
- Set up the agency's first continuous-integration pipeline (Jenkins) and automated deployments that had previously been done by hand over FTP.

SKILLS

Languages: Python (expert), SQL (expert), Go (working knowledge), TypeScript (basic)
Frameworks and tools: FastAPI, Django, Celery, SQLAlchemy, pytest, Hypothesis
Data: PostgreSQL, Redis, Redis Streams, Amazon RDS
Infrastructure: AWS (EC2, RDS, SQS, S3, IAM), Docker, GitHub Actions, Terraform (reading and modifying existing modules)
Practices: incident response, on-call, observability (OpenTelemetry, Prometheus, Grafana), design documents, code review

EDUCATION

BSc (Hons) Computer Science, 2:1 — Aldermoor University, 2011 – 2014
Dissertation: scheduling heuristics for multi-drop delivery routes

OTHER

Speaker, "Outbox tables without tears", regional Python user group meetup, 2023
Volunteer mentor at a code club for secondary school students
