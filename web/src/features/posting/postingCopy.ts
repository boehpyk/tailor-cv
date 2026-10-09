/**
 * **Error B — the API refused `POST` of a job posting**, chosen by `code` (AC-13), so the server's
 * prose (*"Try again in …"*) never reaches the DOM. A 429's sentence names the wait through
 * `retryPhrase(deadlineMs, …)`.
 *
 * T7 SKELETON: returns today's output (the server's message); GREEN replaces it and wires it into
 * `JobPostingPanel`.
 */
// eslint-disable-next-line @typescript-eslint/no-unused-vars -- T7 skeleton; read in GREEN
export function postingRejectionMessage(error: Error, _deadlineMs: number | null): string {
  return error.message;
}
