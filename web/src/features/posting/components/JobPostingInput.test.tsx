import { createRef } from 'react';

import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';

import { JobPostingInput } from './JobPostingInput';

import type { JobPostingInputProps } from './JobPostingInput';

function makeProps(overrides: Partial<JobPostingInputProps> = {}): JobPostingInputProps {
  return {
    mode: 'pasted',
    text: '',
    url: '',
    disabled: false,
    maxCharacters: 30000,
    textareaRef: createRef<HTMLTextAreaElement>(),
    onModeChange: vi.fn(),
    onTextChange: vi.fn(),
    onUrlChange: vi.fn(),
    ...overrides,
  };
}

describe('JobPostingInput', () => {
  it('renders both radios with their labels, checked following the mode prop', () => {
    const { rerender } = render(<JobPostingInput {...makeProps({ mode: 'pasted' })} />);

    expect(screen.getByRole('radio', { name: /paste the text/i })).toBeChecked();
    expect(screen.getByRole('radio', { name: /link to the posting/i })).not.toBeChecked();

    rerender(<JobPostingInput {...makeProps({ mode: 'fetched' })} />);

    expect(screen.getByRole('radio', { name: /paste the text/i })).not.toBeChecked();
    expect(screen.getByRole('radio', { name: /link to the posting/i })).toBeChecked();
  });

  it('shows the textarea in pasted mode and the URL input in fetched mode', () => {
    const { rerender } = render(<JobPostingInput {...makeProps({ mode: 'pasted' })} />);

    expect(screen.getByLabelText(/job posting text/i)).toBeInTheDocument();
    expect(screen.queryByLabelText(/job posting url/i)).not.toBeInTheDocument();

    rerender(<JobPostingInput {...makeProps({ mode: 'fetched' })} />);

    expect(screen.queryByLabelText(/job posting text/i)).not.toBeInTheDocument();
    const urlInput = screen.getByLabelText(/job posting url/i);
    expect(urlInput).toBeInTheDocument();
    expect(urlInput).toHaveAttribute('type', 'url');
  });

  it('shows the live counter formatted with a thousands separator', () => {
    render(
      <JobPostingInput
        {...makeProps({ mode: 'pasted', text: 'x'.repeat(3184), maxCharacters: 30000 })}
      />,
    );

    expect(screen.getByText('3,184 / 30,000')).toBeInTheDocument();
  });

  it('marks the counter as a warning once the text is over the limit', () => {
    const { rerender } = render(
      <JobPostingInput
        {...makeProps({ mode: 'pasted', text: 'x'.repeat(5), maxCharacters: 10 })}
      />,
    );

    expect(screen.getByText('5 / 10')).toHaveClass('text-slate-500');

    rerender(
      <JobPostingInput
        {...makeProps({ mode: 'pasted', text: 'x'.repeat(11), maxCharacters: 10 })}
      />,
    );

    expect(screen.getByText('11 / 10')).toHaveClass('text-red-700');
  });

  it('disables the whole fieldset, including the visible controls, when disabled', () => {
    const { rerender } = render(
      <JobPostingInput {...makeProps({ mode: 'pasted', disabled: true })} />,
    );

    expect(screen.getByRole('radio', { name: /paste the text/i })).toBeDisabled();
    expect(screen.getByRole('radio', { name: /link to the posting/i })).toBeDisabled();
    expect(screen.getByLabelText(/job posting text/i)).toBeDisabled();

    rerender(<JobPostingInput {...makeProps({ mode: 'fetched', disabled: true })} />);

    expect(screen.getByLabelText(/job posting url/i)).toBeDisabled();
  });

  it('calls onModeChange with the target mode when a radio is clicked', async () => {
    const user = userEvent.setup();
    const onModeChange = vi.fn();
    render(<JobPostingInput {...makeProps({ mode: 'pasted', onModeChange })} />);

    await user.click(screen.getByRole('radio', { name: /link to the posting/i }));

    expect(onModeChange).toHaveBeenCalledWith('fetched');
  });

  it('calls onTextChange as the textarea is typed into', async () => {
    const user = userEvent.setup();
    const onTextChange = vi.fn();
    render(<JobPostingInput {...makeProps({ mode: 'pasted', onTextChange })} />);

    await user.type(screen.getByLabelText(/job posting text/i), 'a');

    expect(onTextChange).toHaveBeenCalledWith('a');
  });

  it('calls onUrlChange as the URL input is typed into', async () => {
    const user = userEvent.setup();
    const onUrlChange = vi.fn();
    render(<JobPostingInput {...makeProps({ mode: 'fetched', onUrlChange })} />);

    await user.type(screen.getByLabelText(/job posting url/i), 'h');

    expect(onUrlChange).toHaveBeenCalledWith('h');
  });
});
