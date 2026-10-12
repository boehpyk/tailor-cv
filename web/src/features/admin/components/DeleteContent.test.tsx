import { fireEvent, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { __resetForTests } from '@/features/auth/authStore';
import { createAppQueryClient } from '@/queryClient';
import { callsTo, signInAs, status, stubAccountFetch } from '@/test/accountFetch';
import { ADMIN, ERASURE, TARGET_ID, contentRoutes, world } from '@/test/adminContentFetch';
import { renderWithRouter } from '@/test/render';

import {
  CANCEL_LABEL,
  DELETE_ALREADY_GONE,
  DELETE_CV_DIALOG_TITLE,
  DELETE_CV_LABEL,
  DELETE_ENTRY_DIALOG_TITLE,
  DELETE_ENTRY_LABEL,
  DELETE_IN_PROGRESS,
  deleteCvDialogMessage,
  deletedNotice,
  unlinkFailuresNote,
} from '../adminCopy';

import type { ContentWorld } from '@/test/adminContentFetch';
import type { RouteHandler } from '@/test/accountFetch';

/**
 * T12 RED — slice 4.3, AC-29: *Delete CV* (on the CV page) and *Delete history entry* (on the run
 * page). The dialog names the item and what goes with it; success returns to the account with a
 * notice; 409 and 404 have their own words; not optimistic; a same-tick double submit sends one
 * request. Against T11's skeleton (`DeleteContentButton` renders an empty div) each fails on an
 * absent button.
 */

beforeEach(() => {
  __resetForTests();
});

afterEach(() => {
  vi.unstubAllGlobals();
});

const P = '/api/admin/users/:id';
const ACCOUNT = `/admin/users/${TARGET_ID}`;
const CV_PATH = `${ACCOUNT}/cvs/cv-1`;
const RUN_PATH = `${ACCOUNT}/runs/run-1`;

function open(
  path: string,
  w: ContentWorld = world(),
  overrides: Record<string, RouteHandler> = {},
) {
  const fetch = stubAccountFetch(contentRoutes(w, overrides));
  signInAs(ADMIN);
  const view = renderWithRouter(path, { queryClient: createAppQueryClient() });
  return { fetch, ...view };
}

async function openDialog(trigger: string): Promise<HTMLElement> {
  const button = await screen.findByRole('button', { name: trigger });
  await waitFor(() => {
    expect(button).toBeEnabled();
  });
  await userEvent.setup().click(button);
  return screen.findByRole('alertdialog');
}

/** The dialog's one button that is not Cancel — the label is the component's, the rule is the spec's. */
function confirmIn(dialog: HTMLElement): HTMLElement {
  const buttons = within(dialog)
    .getAllByRole('button')
    .filter((b) => b.textContent !== CANCEL_LABEL);
  expect(buttons).toHaveLength(1);
  return buttons[0] as HTMLElement;
}

describe('the dialogs — AC-29', () => {
  it('Delete CV names the CV and says what goes and that it cannot be undone', async () => {
    open(CV_PATH);

    const dialog = await openDialog(DELETE_CV_LABEL);

    expect(within(dialog).getByText(DELETE_CV_DIALOG_TITLE)).toBeVisible();
    expect(within(dialog).getByText(deleteCvDialogMessage('Backend roles'))).toBeVisible();
    expect(within(dialog).getByRole('button', { name: CANCEL_LABEL })).toBeVisible();
  });

  it('Delete history entry names the posting and date, its exports and its card on the board', async () => {
    open(RUN_PATH);

    const dialog = await openDialog(DELETE_ENTRY_LABEL);

    expect(within(dialog).getByText(DELETE_ENTRY_DIALOG_TITLE)).toBeVisible();
    expect(
      within(dialog).getByText(
        /The entry for Senior Platform Engineer from .+ will be deleted, with its exports and its card on the board\. This cannot be undone\./,
      ),
    ).toBeVisible();
  });

  it('Cancel closes the dialog and sends nothing', async () => {
    const { fetch } = open(CV_PATH);
    const dialog = await openDialog(DELETE_CV_LABEL);

    await userEvent.setup().click(within(dialog).getByRole('button', { name: CANCEL_LABEL }));

    await waitFor(() => {
      expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument();
    });
    expect(callsTo(fetch, 'DELETE', `${P}/base-cvs/cv-1`)).toHaveLength(0);
  });
});

describe('deleting a saved CV — AC-29', () => {
  it('sends the admin DELETE, returns to the account with "Deleted …", and the list refetches', async () => {
    const { fetch, router } = open(CV_PATH);
    const dialog = await openDialog(DELETE_CV_LABEL);

    await userEvent.setup().click(confirmIn(dialog));

    expect(await screen.findByText(deletedNotice('Backend roles'))).toBeVisible();
    expect(router.state.location.pathname).toBe(ACCOUNT);
    const [call] = callsTo(fetch, 'DELETE', `${P}/base-cvs/cv-1`);
    expect(call?.authorization).toBe(`Bearer token-${ADMIN.id}`);
    expect(await screen.findByText('No saved CVs.')).toBeVisible();
    expect(screen.queryByText(unlinkFailuresNote(1))).not.toBeInTheDocument();
  });

  it('an unlink failure is named in the notice, with the orphan sweep', async () => {
    open(CV_PATH, world(), {
      [`DELETE ${P}/base-cvs/:cvId`]: () => Response.json({ file_unlinked: false }),
    });
    const dialog = await openDialog(DELETE_CV_LABEL);

    await userEvent.setup().click(confirmIn(dialog));

    expect(await screen.findByText(unlinkFailuresNote(1))).toBeVisible();
    expect(screen.getByText(deletedNotice('Backend roles'))).toBeVisible();
  });

  it('the kind\'s 404 says "Already deleted."', async () => {
    open(CV_PATH, world(), {
      [`DELETE ${P}/base-cvs/:cvId`]: status(404, 'base_cv_not_found'),
    });
    const dialog = await openDialog(DELETE_CV_LABEL);

    await userEvent.setup().click(confirmIn(dialog));

    expect(await screen.findByText(DELETE_ALREADY_GONE)).toBeVisible();
  });

  it('is not optimistic: the CV stays on screen while the DELETE is pending', async () => {
    let release: (r: Response) => void = () => undefined;
    open(CV_PATH, world(), {
      [`DELETE ${P}/base-cvs/:cvId`]: () =>
        new Promise<Response>((resolve) => {
          release = resolve;
        }),
    });
    const dialog = await openDialog(DELETE_CV_LABEL);

    await userEvent.setup().click(confirmIn(dialog));

    await waitFor(() => {
      expect(release).not.toBe(undefined);
    });
    expect(screen.getByRole('heading', { level: 1, name: 'Backend roles' })).toBeVisible();
    expect(screen.queryByText(deletedNotice('Backend roles'))).not.toBeInTheDocument();
    release(Response.json({ file_unlinked: true }));
  });

  it('a same-tick double submit sends exactly one DELETE (isMutating guard)', async () => {
    let release: (r: Response) => void = () => undefined;
    const { fetch } = open(CV_PATH, world(), {
      [`DELETE ${P}/base-cvs/:cvId`]: () =>
        new Promise<Response>((resolve) => {
          release = resolve;
        }),
    });
    const dialog = await openDialog(DELETE_CV_LABEL);
    const confirm = confirmIn(dialog);

    fireEvent.click(confirm);
    fireEvent.click(confirm);

    await waitFor(() => {
      expect(callsTo(fetch, 'DELETE', `${P}/base-cvs/cv-1`).length).toBeGreaterThan(0);
    });
    expect(callsTo(fetch, 'DELETE', `${P}/base-cvs/cv-1`)).toHaveLength(1);
    release(Response.json({ file_unlinked: true }));
  });
});

describe('deleting a history entry — AC-29', () => {
  it('sends the admin DELETE and returns to the account with "Deleted …"', async () => {
    const { fetch, router } = open(RUN_PATH);
    const dialog = await openDialog(DELETE_ENTRY_LABEL);

    await userEvent.setup().click(confirmIn(dialog));

    expect(await screen.findByText(/^Deleted .*Senior Platform Engineer/)).toBeVisible();
    expect(router.state.location.pathname).toBe(ACCOUNT);
    expect(callsTo(fetch, 'DELETE', `${P}/tailoring-runs/run-1`)).toHaveLength(1);
    expect(await screen.findByText('No tailoring history.')).toBeVisible();
  });

  it('unlink failures are counted in the notice', async () => {
    open(RUN_PATH, world(), {
      [`DELETE ${P}/tailoring-runs/:rid`]: () => Response.json({ ...ERASURE, unlink_failures: 2 }),
    });
    const dialog = await openDialog(DELETE_ENTRY_LABEL);

    await userEvent.setup().click(confirmIn(dialog));

    expect(await screen.findByText(unlinkFailuresNote(2))).toBeVisible();
  });

  it('409 tailoring_run_in_progress: the explanation, the dialog closed, nothing changed', async () => {
    const { router } = open(RUN_PATH, world(), {
      [`DELETE ${P}/tailoring-runs/:rid`]: status(
        409,
        'tailoring_run_in_progress',
        'still working',
        { status: 'running' },
      ),
    });
    const dialog = await openDialog(DELETE_ENTRY_LABEL);

    await userEvent.setup().click(confirmIn(dialog));

    expect(await screen.findByText(DELETE_IN_PROGRESS)).toBeVisible();
    expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument();
    expect(router.state.location.pathname).toBe(RUN_PATH);
    expect(screen.queryByText(/^Deleted /)).not.toBeInTheDocument();
  });

  it('the kind\'s 404 says "Already deleted."', async () => {
    open(RUN_PATH, world(), {
      [`DELETE ${P}/tailoring-runs/:rid`]: status(404, 'tailoring_run_not_found'),
    });
    const dialog = await openDialog(DELETE_ENTRY_LABEL);

    await userEvent.setup().click(confirmIn(dialog));

    expect(await screen.findByText(DELETE_ALREADY_GONE)).toBeVisible();
  });
});
