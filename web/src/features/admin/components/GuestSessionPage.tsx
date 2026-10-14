import { useParams } from 'react-router';

export interface GuestSessionPageProps {
  /** The signed-in admin, from `AccountScope`; roots every query key (AC-40). */
  readonly actorId: string;
}

/**
 * `/admin/guests/:sessionId` (AC-33): `useGuestSession`; loading / gone / error + Retry / the
 * overview, three content sections and `DeleteGuestSessionButton`.
 *
 * SKELETON (T20): an empty, data-attributed placeholder; T22 builds the views.
 */
export function GuestSessionPage({ actorId }: GuestSessionPageProps): React.JSX.Element {
  const { sessionId = '' } = useParams();
  return <div data-admin-stub="guest-session" data-actor={actorId} data-session={sessionId} />;
}
