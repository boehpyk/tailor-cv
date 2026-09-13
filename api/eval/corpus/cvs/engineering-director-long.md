HELENA MARCHETTI-VOS
Director of Engineering

Edinburgh, United Kingdom (open to hybrid or remote roles across the UK)
helena.marchetti-vos@example.com · 07700 900364 · helena-mv.example

PROFILE

Engineering leader with twenty years of experience, the last eleven managing teams and managers, and the last nine in regulated health software. I build organisations that ship safely and often: clear ownership, small teams with real autonomy, boring and well-rehearsed releases, and a culture where an incident review is a learning exercise rather than a trial. I am at my best in the messy middle of a company's growth, somewhere between forty and one hundred and fifty engineers, when the practices that worked for one team stop working for seven and the organisation needs structure without losing its speed. I stay technical enough to challenge an architecture proposal and to read the pull request behind a production incident, and I care a great deal about hiring fairly and growing managers from within.

CAREER HIGHLIGHTS

- Grew the engineering organisation at Calloway Health Systems from 21 to 58 engineers across seven teams while cutting the median lead time for changes from nine days to under two.
- Led the technical and organisational work that took a clinical records product through its first UK medical device registration (UKCA, Class I) without delaying the product roadmap by more than one quarter.
- Replaced a quarterly, weekend-long release process with continuous delivery behind feature flags at Ferngate Clinical Software; production incidents caused by releases fell by roughly 70% over eighteen months.
- Built and ran an internal engineering-management programme that has produced nine engineering managers from existing staff, seven of whom are still in post.
- Shipped four console and PC titles as an engineering manager at Obsidian Lantern Games, including the studio's first live-service game.

EXPERIENCE

Director of Engineering — Calloway Health Systems, Edinburgh
May 2019 – present

Calloway builds electronic patient-record and care-coordination software used by community health providers and care homes across the UK, and employs around 260 people. I report to the Chief Technology Officer and lead all product engineering: seven teams, six engineering managers, two staff engineers and 58 engineers in total, with an annual people and tooling budget of about £6.4 million.

- Reorganised engineering from three large component teams into seven stream-aligned product teams plus a platform team, with explicit ownership of every service and every on-call rota; the proportion of work items blocked on another team fell from 38% to 11% within a year.
- Introduced DORA metrics as a shared, non-punitive dashboard. Median lead time for changes went from nine days to 1.6 days and deployment frequency from weekly to several times a day, while the change failure rate stayed below 10%.
- Sponsored the migration from a single-tenant hosting model on virtual machines to a multi-tenant platform on managed Kubernetes in a UK cloud region, delivered over 20 months with no customer-visible downtime; hosting cost per customer fell by 34%.
- Own engineering's part of the company's clinical safety management system (DCB0129), working with the Clinical Safety Officer to build hazard logging into the design review process rather than bolting it on at release time.
- Led engineering through the product's first UKCA Class I medical device registration, including the design history file, software lifecycle documentation aligned to IEC 62304, and a traceability matrix generated from our issue tracker instead of maintained by hand.
- Worked with the Head of Security to achieve ISO 27001 certification and "standards met" status in the NHS Data Security and Protection Toolkit in two consecutive years.
- Designed a transparent engineering career framework with parallel management and individual-contributor tracks up to principal engineer, and calibrated it with a pay-banding exercise that closed an unexplained gender pay gap in engineering.
- Rebuilt hiring around structured interviews with written rubrics, a paid take-home exercise capped at three hours, and interviewer training. Offer acceptance rose from 61% to 84%, and the share of women in engineering from 17% to 31%.
- Run the incident management programme: severity definitions, an incident commander rota, and blameless reviews published internally. Mean time to restore for severity-one incidents fell from about four hours to 47 minutes.
- Represent engineering to the board every quarter, and led the technology section of the Series C due diligence.
- Mentor the six engineering managers and two staff engineers personally, and hold a skip-level conversation with every engineer at least once a quarter.

Senior Engineering Manager — Ferngate Clinical Software, Glasgow
August 2015 – April 2019

Ferngate made laboratory information and results-reporting software for NHS pathology departments and private laboratories. I led the results platform group of three teams, three team leads and 24 engineers, reporting to the VP of Engineering.

- Replaced a quarterly release train, with weekend deployments and a manual regression pack of about 1,100 test cases, with continuous delivery behind feature flags; release-related production incidents fell by roughly 70% over eighteen months.
- Led the rewrite of the HL7 v2 results interface engine from a legacy Delphi application into a set of Java services, using a strangler-pattern migration that let 42 laboratories move one at a time.
- Introduced contract testing between the results platform and the ordering system, removing the need for a shared staging environment that had become a release bottleneck.
- Worked closely with the Quality and Regulatory team on ISO 13485 audits; completed internal auditor training and led engineering's evidence preparation for two external surveillance audits with no major non-conformities.
- Created the company's first on-call policy, with paid on-call allowances and a maximum of one week in five, after an engagement survey identified out-of-hours support as the leading cause of attrition.
- Promoted three engineers into team-lead roles and designed a six-month transition plan for each of them.
- Reduced voluntary engineering attrition in the group from 24% to 9% over two years.
- Partnered with product management to introduce quarterly planning based on outcomes rather than feature lists.

Engineering Manager — Obsidian Lantern Games, Dundee
March 2012 – July 2015

An independent games studio of about 90 people. I managed the gameplay and online services engineers, up to 16 people across two titles, reporting to the Head of Technology.

- Shipped four titles on console and PC, including the studio's first live-service game, which ran seasonal content updates every six weeks for three years.
- Built the online services team from scratch: matchmaking, player accounts, telemetry, and a live-operations tool used by the community team.
- Introduced automated build verification for three platforms, reducing broken builds reaching the test team from several a week to about one a month.
- Ran crunch-reduction planning with production: scope reviews at every milestone and a rule that overtime had to be requested and was reviewed weekly. Average overtime in the last three months before launch fell from 14 to 4 hours a week between the first and the last title.
- Managed the relationship with an external porting partner for one platform.
- Hired eleven engineers, including the studio's first two graduate-programme hires.

Technical Lead — Rookwood Telematics, Newcastle upon Tyne
January 2009 – February 2012

Rookwood provided vehicle-tracking and driver-behaviour software for commercial fleets. I led a team of six engineers building the fleet dashboard and the data-ingestion services.

- Designed the ingestion pipeline for GPS and accelerometer data from about 30,000 vehicles, moving from a batch upload every fifteen minutes to near-real-time processing through a message queue.
- Led the rebuild of the customer dashboard as a single-page web application, which became the company's main sales demonstration.
- Introduced code review, a continuous-integration server and automated tests to a codebase that had none; the test suite grew to about 3,000 tests.
- Worked directly with three large fleet customers to define driver-scoring features, and presented the roadmap at the annual customer day.
- Acted as the escalation point for production issues and set up the first monitoring and alerting.

Software Engineer, later Senior Software Engineer — Pelham Brothers Insurance, Edinburgh
September 2005 – December 2008

Joined the graduate development programme of a mid-sized general insurer and worked on the policy administration and online quote systems.

- Built pricing-rule components for the online car-insurance quote engine in Java, handling around 15,000 quotes a day.
- Rewrote the overnight policy-renewal batch job, reducing its run time from seven hours to under two and ending a recurring breach of the morning service window.
- Promoted to Senior Software Engineer in 2007 and led a team of three on the integration with two price-comparison websites.
- Wrote the team's first coding standards and ran lunchtime sessions on unit testing.

SELECTED PROJECTS

Multi-tenant platform migration (Calloway Health Systems, 2020 – 2022). Executive sponsor and escalation point for a 20-month programme that moved 140 customer deployments from single-tenant virtual machines onto a shared Kubernetes platform. Defined the tenancy model with the staff engineers, insisted on per-tenant encryption keys and audit logging from the first release, and set up a migration factory in which a small rotating squad moved batches of ten customers a fortnight. The last customer moved three weeks ahead of plan.

Care-coordination messaging (Calloway Health Systems, 2021). Led the engineering side of a partnership with two regional health boards to exchange care-plan updates between community nurses and care homes using FHIR messaging. Agreed the integration testing approach with the boards' own suppliers and put a joint incident process in place before go-live.

Results interface engine replacement (Ferngate Clinical Software, 2016 – 2018). Programme lead for replacing the legacy interface engine. Designed the per-laboratory cut-over runbook; a shadow-running period in which the old and new engines processed the same messages while a comparison tool flagged every difference; and a rollback path that was rehearsed for every laboratory. Forty-two laboratories migrated with two rollbacks, both inside the rehearsed window.

Live-service launch (Obsidian Lantern Games, 2014). Led engineering readiness for the studio's first live-service title: load testing matchmaking to five times the forecast launch concurrency, a staged regional launch, and a war-room rota for the first two weeks. Peak concurrency reached 62,000 players with no unplanned downtime.

Engineering management programme (Calloway Health Systems, 2020 – present). Designed and run a nine-month programme for engineers considering management: a trial period leading a small project, paired one-to-ones with an experienced manager, monthly workshops on feedback, hiring and performance, and an explicit, no-stigma route back to the individual-contributor track. Nine managers have come through it. Two chose to return to engineering roles, which I count as the programme working.

LEADERSHIP PRACTICE

Organisation design: stream-aligned teams with clear service ownership, a platform team with an internal product mindset, and explicit ways for teams to interact. I revisit the structure every six months rather than waiting for it to fail.

Managing managers: weekly one-to-ones, a written expectations document for every management role, a monthly managers' forum for discussing difficult cases, and a shared calibration process for performance reviews.

Delivery: small batches, trunk-based development, feature flags, and service-level objectives agreed with product management. I treat delivery metrics as a conversation starter for a team, never as a target set for it.

Regulated software: pragmatic quality management that lives inside the engineering workflow. Hazard logs are linked to tickets, documentation is generated from source where possible, and audits are treated as routine rather than as an event.

Hiring and inclusion: structured interviews, clear rubrics, diverse panels, fair take-home exercises and published salary bands.

Budget and vendors: ownership of engineering headcount planning, cloud spend and tooling contracts. Led the renegotiation of the main cloud contract at Calloway Health Systems, saving about £410,000 over three years.

SELECTED TALKS AND WRITING

- "Continuous delivery in a regulated world", keynote at a UK health-technology engineering conference, 2023
- "The first hundred days of a new engineering manager", talk at a regional engineering leadership meetup, 2021
- "Strangling an interface engine, one laboratory at a time", conference talk about the HL7 migration, 2018
- Regular writing on engineering leadership at helena-mv.example, including a widely shared series on writing incident reviews

EDUCATION

MSc Software Engineering, Distinction — Easterbrook Polytechnic, 2004 – 2005
Dissertation: static analysis of concurrency defects in Java programs

BSc (Hons) Mathematics, First Class — University of Wyverne, 2001 – 2004

TRAINING AND CERTIFICATIONS

- Clinical risk management training for manufacturers of health IT systems (DCB0129 and DCB0160), 2020
- ISO 13485 internal auditor training, 2017
- ISO/IEC 27001 foundation, 2021
- Twelve-month executive coaching programme for technology leaders, 2022

TECHNICAL BACKGROUND

Hands-on history: Java (fifteen years), Python, SQL, JavaScript and TypeScript, and some C++ from the games years.
Architecture: service-oriented and event-driven systems, HL7 v2 and FHIR integration, multi-tenant SaaS.
Platform: AWS and a UK sovereign cloud region, Kubernetes, Terraform, PostgreSQL, Kafka, observability with OpenTelemetry.
I no longer write production code day to day, but I review architecture decision records, pair with staff engineers on technical strategy, and read incident timelines in detail.

COMMUNITY

- Mentor on a programme for women moving into engineering leadership, run by a professional body, since 2017
- Governor at a primary school in Edinburgh, 2018 – 2022, responsible for the school's technology strategy

LANGUAGES

English (native), Italian (fluent), Dutch (conversational)

REFERENCES

Available on request.
