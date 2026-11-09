/**
 * Mirrors `api/src/tailorcraft/infrastructure/api/schemas/billing.py`, field for field (slice 5.2,
 * technical plan §4). Hand-written, like every other mirror here.
 *
 * **The balance is the server's.** `balance` and `out_of_credits` arrive computed; the client never
 * sums `entries` (they stop at 100) and never decides by itself that an account may tailor.
 */

/** A pack id. A closed set: its credits and price are the server's code fact, read from `packs`. */
export type PackId = 'pack_3' | 'pack_10';

/** One pack's terms. `price_cents` is US cents (`500` is $5). Nothing is sold in this slice. */
export interface PackTerms {
  readonly id: PackId;
  readonly credits: number;
  readonly price_cents: number;
}

/** What a ledger line records. */
export type CreditEntryKind = 'welcome' | 'existing_account' | 'grant' | 'charge' | 'refund';

/** One ledger line, newest first. `credits` is signed (`-1` for a charge). */
export interface CreditEntry {
  readonly kind: CreditEntryKind;
  readonly credits: number;
  /** Set for a `charge` or a `refund` only. */
  readonly tailoring_run_id: string | null;
  readonly at: string;
}

/** The one-time notice an account shows until its first charge. */
export type CreditNotice = 'credits_introduced' | 'first_credit';

/** `GET /api/me/credits`. */
export interface Credits {
  readonly balance: number;
  readonly out_of_credits: boolean;
  readonly notice: CreditNotice | null;
  readonly payment_notice_opted_in: boolean;
  readonly packs: readonly PackTerms[];
  /** Newest first, at most 100. */
  readonly entries: readonly CreditEntry[];
}

/** The answer to a pack click and to the opt-in `PUT`. */
export interface PaymentNoticeState {
  readonly payment_notice_opted_in: boolean;
}
