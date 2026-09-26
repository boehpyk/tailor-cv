import { useEffect, useState } from 'react';
import { useLocation, useNavigate } from 'react-router';

import { homeNoticeFrom } from '../homeNotice';

/**
 * The one-off notice on `/` after an account deletion (AC-40): "Your account and saved CVs were
 * deleted.", carried there by `DeleteAccountSection` in the router's location state.
 *
 * **Shown once.** History state survives a reload and the back button, so a notice read straight
 * from `location.state` on every render would greet the user again tomorrow. The notice is read
 * **once**, into this component's own state (the lazy initializer — a snapshot on purpose, not a
 * copy kept in sync), and the history entry is then replaced with the same URL and no state. That
 * replacement is the effect's whole job: it synchronizes the browser's history with "the notice has
 * been shown", which is something outside React.
 */
export function AccountDeletedNotice(): React.JSX.Element | null {
  const location = useLocation();
  const navigate = useNavigate();
  const [notice] = useState(() => homeNoticeFrom(location.state));
  const carriesNotice = homeNoticeFrom(location.state) !== null;

  useEffect(() => {
    if (carriesNotice) {
      void navigate(
        { pathname: location.pathname, search: location.search, hash: location.hash },
        { replace: true, state: null },
      );
    }
  }, [carriesNotice, navigate, location.pathname, location.search, location.hash]);

  if (notice === null) {
    return null;
  }
  return (
    <p
      role="status"
      className="mb-8 rounded-lg border border-slate-200 bg-slate-50 p-4 text-sm text-slate-800"
    >
      {notice}
    </p>
  );
}
