/**
 * Hand a `Blob` to the browser as a file download, under a name we choose.
 *
 * This is the last step of every download in the app, and it is deliberately the *only* thing in
 * the codebase that touches `URL.createObjectURL` or builds an anchor. The bytes arrived through
 * the typed client (`requestBlob`), which is what made a 401 an `ApiError` instead of a JSON file
 * named `tailored-cv.pdf`; this function is what turns the bytes that *did* arrive into a file
 * (AC-42).
 *
 * **Not a `useEffect`.** Saving a file is a side effect of a user action, not a synchronization
 * with something outside React, and it happens once per click rather than once per render. It is
 * called from a mutation's `mutationFn` (`useDownload`), where the click that caused it is still on
 * the stack. An effect here would need a piece of state to trigger it, a cleanup to avoid firing
 * twice, and a StrictMode rehearsal would save the file twice in development.
 *
 * **Nothing is stored.** The object URL exists for the duration of one `click()` and the `Blob`
 * lives in a local. Nothing reaches `localStorage`, `sessionStorage` or IndexedDB — this is a
 * stranger's employment history, and a grep test enforces the rule across the whole feature folder.
 *
 * @param blob the bytes, exactly as the API sent them — never re-encoded here
 * @param filename the name the file lands under; a constant keyed on (document, format), never the
 *   user's text. The server's `Content-Disposition` carries the same constant and is not parsed
 *   back out (see `requestBlob`).
 */
export function saveBlob(blob: Blob, filename: string): void {
  const url = URL.createObjectURL(blob);
  try {
    // Detached: never appended to the document. Appending was once required by Firefox and no
    // longer is, and an anchor that is added to the DOM is an anchor some path can fail to remove —
    // one more thing to clean up on a throw, in a function whose whole job is cleaning up.
    const anchor = document.createElement('a');
    anchor.href = url;
    anchor.download = filename;
    anchor.click();
  } finally {
    // **In a `finally`, and that is the point of the whole shape.** An object URL is a reference
    // the document holds to the blob's bytes, and it is released only by this call or by the page
    // going away — so a PDF downloaded and revoked nowhere is a few hundred kilobytes pinned for
    // as long as the tab is open, four formats and two documents at a time. Putting the revoke
    // after `click()` on the happy path alone leaks exactly when something went wrong.
    //
    // Revoking synchronously right after `click()` is safe: the click starts the download in the
    // same task, and the browser has taken its own reference to the blob by the time this runs.
    URL.revokeObjectURL(url);
  }
}
