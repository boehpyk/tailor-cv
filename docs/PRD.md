# TailorCraft (AI-Powered CV & Cover Letter Customizer)

**Version:** v1  
**Status:** Draft

## 1. Product Overview

TailorCraft is a web application designed to allow job seekers to dynamically tailor their CV and cover letter to match specific job descriptions using LLM API processing. It enables users to upload a base resume, input job posting details via text or URL, edit generated content in a rich visual editor, and download finalized files in Text, PDF, DOCX, or Markdown formats—with optional account registration for application tracking and persistent data storage. Additionally, the codebase serves as an educational sandbox for learning full-stack development with Python and React.

## 2. Goals

### Business Goals
* **Personal & Social Efficiency:** Enable immediate deployment for the founder and core friend group to accelerate re-employment.
* **Monetization Readiness:** Validate user demand and willingness-to-pay before transitioning to a paid subscription model.

### User Goals
* **Rapid Application Customization:** Drastically cut down time spent manually tailoring CVs and cover letters while maximizing job requirement alignment.
* **Developer Skill Acquisition (Founder Goal):** Serve as a practical, real-world project to master idiomatic Python (backend API/worker design) and React (frontend architecture, state management, and component patterns).

### Non-Goals
* Automated job application submission or mass web-scraping platforms.
* Complex multi-page graphical/visual layout design engine (focusing instead on standard structured document exports).

## 3. User Personas

### Key User Types
* **Guest User:** A job seeker performing one-off application tailoring without creating an account.
* **Registered User:** A job seeker who registers to store base CVs, manage application history, and access saved profiles across sessions.

### Basic Persona Details
* **Persona:** Alex, Tech & General Professional
* **Context:** Currently unemployed or actively job hunting, applying to 10+ tailored roles weekly.
* **Pain Point:** Spends 30–45 minutes per application tweaking resumes and writing custom cover letters, experiencing severe job-search fatigue and lower submission volume.

### Role-Based Access
* **Guest User:** Upload base CV (in-memory/1-day transient local storage), input job description text/URL, tailor documents via LLM, edit via rich text interface, and download files (PDF, DOCX, TXT, Markdown).
* **Registered User:** All Guest privileges, plus persistent account storage for base CVs, application history dashboard, and saved template configurations.

## 4. User Stories

* **US-1:** As a guest or registered user, I want to upload my base CV (PDF, DOCX, TXT) so that the system can extract my background details.
* **US-2:** As a guest or registered user, I want to paste a job description or provide a URL so that the system can extract the job requirements.
* **US-3:** As a guest or registered user, I want the AI to generate a tailored CV and cover letter matching the targeted job description.
* **US-4:** As a guest or registered user, I want to edit the AI-generated CV and cover letter in a rich visual editor before downloading so that I can tweak content and layout styling manually.
* **US-5:** As a guest or registered user, I want to download my finalized CV and cover letter in Text, PDF, DOCX, or Markdown formats so that I can submit applications immediately.
* **US-6:** As a job seeker, I want to optionally register/login so that I can persist my base CV and save past tailored applications.
* **US-7:** As a developer/learner, I want a modular, idiomatic Python and React codebase with clear domain boundaries so that I can learn architectural patterns and software development best practices while building features.

## 5. Functional Requirements

1. **FR-1 (Parsing):** System shall extract plain text from uploaded PDF, DOCX, and TXT base CV files (implements US-1).
2. **FR-2 (Job Scraping & Fallback):** System shall accept raw text input or scrape main job posting text from a URL. If scraping fails (e.g., paywalls or anti-bot blocks), the UI shall display a flash error message prompting manual copy-paste input (implements US-2).
3. **FR-3 (LLM Generation):** System shall call an LLM API (Gemini API) using structured prompts to return tailored CV and cover letter content (implements US-3).
4. **FR-4 (Visual Editor):** System shall render generated content in a React rich visual editor with separate tabs for CV and Cover Letter editing (implements US-4).
5. **FR-5 (Export Engine):** System shall render/convert edited documents into PDF, DOCX, Markdown, and plain Text files for download using a background worker queue for heavy conversions (implements US-5).
6. **FR-6 (Auth & Persistence):** System shall support optional user authentication (JWT/Session) to persist base CVs and application history in PostgreSQL, while enforcing a 1-day retention policy for guest user data (implements US-1, US-6).
7. **FR-7 (Educational Codebase Architecture):** Backend and frontend codebases shall follow clean separation of concerns (e.g., layered Python architecture with type hints/Pydantic models, custom React hooks, reusable UI components) to serve as a high-quality pedagogical reference for Python and React learning (implements US-7).

## 6. User Experience

### Entry Points & First-Time User Flow
* **Landing Interface:** Clean dual-tab workspace (Tab 1: Drag-and-drop File Upload for Base CV; Tab 2: Text/URL Input for Job Description).
* **Guest First-Run:** Guest drags base CV into Tab 1, pastes job URL/text in Tab 2, and clicks "Tailor Application". Post-generation, a soft registration prompt appears: *"Save your base CV and tailoring history for future applications."*

### Core Experience
* **Processing State:** Progress bar displays real-time status updates (*Extracting CV → Fetching Job → Tailoring with LLM*).
* **Completion View:** Direct download links for supported formats (Text, PDF, DOCX, Markdown) alongside an "Edit" button.
* **Editing Workspace:** Tabbed rich editor interface allowing fluid navigation between **CV Editor** and **Cover Letter Editor**.

### Advanced Features & Edge Cases
* **Scraping Block:** Displays global error flash message (*"Unable to extract job text from URL. Please copy and paste the job description directly."*).
* **LLM Timeout/Rate Limit:** Displays retry indicator with exponential backoff prompt.

### UI/UX Highlights
* Dual-tab entry workflow (Upload vs Raw Text/URL).
* Tabbed output editor (CV vs Cover Letter).
* Zero side-by-side comparison bloat—focused strictly on editing and fast exporting.

## 7. Narrative

Alex recently lost his software engineering job and needs to apply to dozens of tailored roles weekly without spending hours tweaking each application. Visiting TailorCraft as a guest, Alex drops his base PDF resume into the upload tab, switches to the Job Description tab to paste a URL from a tech job board, and clicks "Tailor Application". While a progress bar tracks extraction and LLM prompt processing, the app generates a customized CV and cover letter matching the role's primary requirements.

Once processing completes, Alex clicks "Edit" to review the draft in the rich visual editor, toggling between the CV and Cover Letter tabs to adjust two bullet points. Satisfied, Alex exports both files in PDF and DOCX formats with a single click. A banner prompts Alex to create an account so he won't have to re-upload his base CV for his next 10 applications—he registers in under a minute and returns to his job search. Meanwhile, as the maintainer, you use the codebase to study clean Python FastAPI endpoints and modern React state management patterns in a production-like context.

## 8. Success Metrics

| Metric | Type | Baseline | Target | Timeframe |
| :--- | :--- | :--- | :--- | :--- |
| **Friend Group Onboarding** | Business | 0 | 100% active adoption within core network | Week 1 post-launch |
| **CV Tailoring Time** | User | ~30–45 mins (manual) | < 2 minutes per application | Launch |
| **Document Export Success** | Technical | _TBD_ | > 98% successful generation without errors | Month 1 |
| **LLM Processing Latency** | Technical | _TBD_ | < 15 seconds end-to-end | Launch |
| **Learning Milestone Progress** | User | Beginner / Intermediate | Full-stack delivery of Python API & React app | Post-Phase 1 |

## 9. Technical Considerations

### Integration Points
* **LLM Provider API:** Direct integration with Google Gemini API for prompt-driven text tailoring and structured output extraction.

### Data Storage & Privacy
* **Database:** PostgreSQL for user accounts, persistent metadata, base CVs, and application history.
* **File Storage:** Local storage for uploaded/generated documents.
* **Guest Retention:** Automated background cleaner enforcing a 1-day data retention/purge policy for guest sessions.

### Scalability & Performance
* **Target Latency:** End-to-end LLM processing under 15 seconds.
* **Worker Queue:** Asynchronous Celery + Redis task queue handling document generation and PDF rendering to keep the API responsive.

### Codebase & Pedagogical Considerations
* **Python Stack:** FastAPI with Pydantic schemas, SQLAlchemy/SQLModel for ORM data handling, and Celery tasks for background jobs. Emphasize strict typing, async routes, and clean domain isolation.
* **React Stack:** Vite + React (TypeScript or JavaScript with JSDoc), functional components, custom hooks for API interaction, and Tailwind CSS for utility-first styling.

### Potential Challenges
* **Web Scraping Resistance:** Anti-bot or Cloudflare protection blocking job URL scraping. Handled via UI flash message prompting raw text copy-pasting.
* **Learning Curve Balancing:** Balancing feature speed with writing clean, well-structured Python/React code (mitigated by building modularly in 3 distinct phases).

## 10. Build Phases

### Phase 1: Core Foundation & MVP (Python + React Learning Core)
* Dockerized environment setup with Python backend (FastAPI) and React frontend (Vite).
* Document extraction engine (PDF/DOCX/TXT text parsing) and Gemini LLM prompt integration.
* Job description input handling (URL scraping with copy-paste fallback).
* Guest workspace flow: Base CV Upload → LLM Tailoring → React Rich Text Editor (Tabbed CV/Cover Letter) → Multi-format Exporter (PDF via Celery/Redis, DOCX, TXT, Markdown).
* Automated 1-day guest data purge policy.

### Phase 2: Authentication & Application Persistence
* PostgreSQL setup with SQLAlchemy ORM models and Alembic migrations.
* Optional registration and JWT auth flow (handling auth state cleanly in React).
* Persistent base CV storage and application history dashboard for registered users.
* Post-generation registration call-to-action banner.

### Phase 3: Application Tracking & Advanced Features
* Job application tracking dashboard/Kanban board.
* Visual PDF layout styling templates.
* Advanced rate limit/retry UI feedback systems.

---

## Review Notes

### Weak Spots
* **Export Success Rate & LLM Latency Baselines:** Marked as `_TBD_` prior to initial benchmark runs in pre-production environment.
* **URL Scraping Reliability:** Web scraping without third-party proxies will fail on heavily protected job boards (e.g., LinkedIn, Indeed); manual copy-paste fallback serves as the primary safeguard.

### Suggested Validations
1. **Parsing Robustness Test:** Benchmark text extraction across 20+ varied PDF/DOCX resume layouts to identify rendering edge cases before launch.
2. **Prompt Quality Tuning:** Validate Gemini output accuracy and formatting consistency against 10 real-world tech job descriptions.
3. **Celery/Redis PDF Load Test:** Test concurrent PDF generation tasks under worker queue load to ensure response times remain within targets.
4. **Code Architecture Review:** Ensure FastAPI route handlers and React component trees remain modular so learning full-stack concepts remains manageable during Phase 1 development.