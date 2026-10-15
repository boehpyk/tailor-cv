import { Link, useLocation } from 'react-router';

import { ADMIN_NAV_LABEL, NAV_LABELS } from '../adminCopy';

type Area = keyof typeof NAV_LABELS;

const LINKS: readonly (readonly [Area, string])[] = [
  ['users', '/admin'],
  ['guests', '/admin/guests'],
  ['storage', '/admin/storage'],
];

/** Which area a path is in: `/admin` and `/admin/users/…` are Users; anything else under `/admin`
 * (the not-found page) is none. */
function areaOf(pathname: string): Area | null {
  const rest = pathname.replace(/^\/admin\/?/, '');
  if (rest === '' || rest === 'users' || rest.startsWith('users/')) return 'users';
  if (rest === 'guests' || rest.startsWith('guests/')) return 'guests';
  if (rest === 'storage' || rest.startsWith('storage/')) return 'storage';
  return null;
}

/**
 * *Users* · *Guests* · *Storage* (AC-31): above every `/admin` page, `aria-current="page"` on the
 * current area. Not `NavLink`: `/admin` is a prefix of every area, and Users also owns
 * `/admin/users/…`, so the area is decided here, once.
 */
export function AdminNav(): React.JSX.Element {
  const area = areaOf(useLocation().pathname);
  return (
    <nav aria-label={ADMIN_NAV_LABEL} className="mb-6">
      <ul className="flex flex-wrap gap-x-4 gap-y-1 text-sm">
        {LINKS.map(([key, to]) => (
          <li key={key}>
            <Link
              to={to}
              aria-current={area === key ? 'page' : undefined}
              className="text-slate-700 underline-offset-2 hover:underline aria-[current=page]:font-semibold aria-[current=page]:text-slate-900 aria-[current=page]:underline"
            >
              {NAV_LABELS[key]}
            </Link>
          </li>
        ))}
      </ul>
    </nav>
  );
}
