import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import { UploadErrorNotice } from './UploadErrorNotice';

describe('UploadErrorNotice', () => {
  it('renders the preValidation message alone, in an alert', () => {
    render(
      <UploadErrorNotice kind="preValidation" message="Please choose a PDF, DOCX or TXT file." />,
    );

    const alert = screen.getByRole('alert');
    // No action hint is computed for error A — the message is the entire notice.
    expect(alert.textContent).toBe('Please choose a PDF, DOCX or TXT file.');
  });

  it("renders the apiError message verbatim, with a known code's action hint as its own separate text", () => {
    render(
      <UploadErrorNotice
        kind="apiError"
        message="We only accept PDF, DOCX or TXT files."
        status={415}
        code="unsupported_format"
      />,
    );

    // Each queried on its own exact text — if the hint had been concatenated onto the message
    // (rather than rendered as its own node) neither exact-text query would match.
    expect(screen.getByText('We only accept PDF, DOCX or TXT files.')).toBeInTheDocument();
    expect(screen.getByText('Try a PDF, DOCX or TXT file.')).toBeInTheDocument();
  });

  it('renders only the server message for an apiError code with no known hint', () => {
    render(
      <UploadErrorNotice
        kind="apiError"
        message="Something went wrong."
        status={422}
        code="missing_file"
      />,
    );

    const alert = screen.getByRole('alert');
    expect(alert.textContent).toBe('Something went wrong.');
  });

  it('renders a temporary-problem hint for a 5xx apiError with no known code', () => {
    render(
      <UploadErrorNotice kind="apiError" message="Internal error." status={503} code={null} />,
    );

    expect(screen.getByText('Internal error.')).toBeInTheDocument();
    expect(
      screen.getByText('This looks like a temporary problem — try again shortly.'),
    ).toBeInTheDocument();
  });

  it('renders the extractionFailed message alone, in an alert', () => {
    const message = "We saved your file but couldn't read any text from it — it looks like a scan.";
    render(<UploadErrorNotice kind="extractionFailed" message={message} />);

    const alert = screen.getByRole('alert');
    expect(alert.textContent).toBe(message);
  });
});
