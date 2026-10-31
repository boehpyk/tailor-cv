import { Link } from 'react-router';

import type { ScopeMap } from '@/features/scope/scopeMap';

import {
  ACCOUNT_BUSY,
  FAILED,
  GUEST_BUSY,
  GUEST_LIMIT,
  REFUSED,
  SESSION_EXPIRED,
  SIGN_UP_LINK,
  accountLimit,
  limitWhen,
} from '../fitScoreCopy';

import type { FitScoreFailureView } from '../fitScoreView';

export interface FitScoreFailureProps {
  /** `held` is not a failure sentence: the panel renders it as a disabled button and its reason. */
  readonly view: Exclude<FitScoreFailureView, { readonly kind: 'held' }>;
  /** Picks the limit and busy sentences: a guest is offered `/register`, an account is not. */
  readonly scope: ScopeMap['kind'];
  /** When the refusal arrived (`useErrorHold`'s `receivedAtMs`), so the limit's time is fixed text. */
  readonly receivedAtMs: number;
  /** A guest's 401 `guest_session_expired`: `failed` says what tailoring says instead. */
  readonly sessionEnded: boolean;
}

/**
 * A request that finished without a score (presentational): `failed`, `refused`, `limit` or `busy`,
 * each its own sentence in one alert. The panel puts *Try again* under `failed`; the others offer no
 * button, since the same request would be refused again. `limit` and `busy` are not faults, so they
 * use the brand tint, not danger.
 */
export function FitScoreFailure({
  view,
  scope,
  receivedAtMs,
  sessionEnded,
}: FitScoreFailureProps): React.JSX.Element {
  const guest = scope === 'guest';
  let sentence: string;
  let signUp = false;
  let fault = true;
  switch (view.kind) {
    case 'failed':
      sentence = sessionEnded ? SESSION_EXPIRED : FAILED;
      break;
    case 'refused':
      sentence = REFUSED[view.code];
      break;
    case 'limit':
      sentence = guest ? GUEST_LIMIT : accountLimit(limitWhen(view.retryNotBefore, receivedAtMs));
      signUp = guest;
      fault = false;
      break;
    case 'busy':
      sentence = guest ? GUEST_BUSY : ACCOUNT_BUSY;
      signUp = guest;
      fault = false;
      break;
  }
  return (
    <div
      role="alert"
      className={
        fault
          ? 'space-y-2 rounded-lg border border-danger-200 bg-danger-50 px-4 py-3'
          : 'space-y-2 rounded-lg border border-slate-200 bg-brand-50 px-4 py-3'
      }
    >
      <p className={`text-sm font-medium ${fault ? 'text-danger-600' : 'text-brand-700'}`}>
        {sentence}
      </p>
      {signUp && (
        <Link
          to="/register"
          className="inline-flex min-h-11 items-center text-sm font-semibold text-brand-700 underline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand-600 sm:min-h-9"
        >
          {SIGN_UP_LINK}
        </Link>
      )}
    </div>
  );
}
