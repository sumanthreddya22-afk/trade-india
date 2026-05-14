# Tax-lot identification policy (TEMPLATE)

When a position is partially or fully closed, the lot identification
method determines which shares are deemed "sold" — which drives the
cost basis and therefore the realized P&L for tax purposes.

Alpaca defaults to **FIFO** (First In, First Out) unless the account
holder elects otherwise. The bot's `position_snapshot` table records
the aggregate; it does not pick lots.

## Decision (operator fills in)

- [ ] FIFO (default; simplest; matches Alpaca default)
- [ ] LIFO (Last In, First Out)
- [ ] Specific identification (manually choose lot per close)
- [ ] Highest cost (tax-loss harvest preferred)

**Selected:** _____

## Why this choice

(Document why. Examples: short-term gains preference, harvest planning,
matches existing portfolio outside the bot, simplicity.)

## Implementation

If FIFO: nothing to change — Alpaca and IRS default align.

If non-FIFO: 
1. File the election with Alpaca via their support form before
   trading begins.
2. Set the per-account lot method in your Alpaca profile.
3. Document the date the election took effect in this file.
4. Confirm year-end 1099 reflects the chosen method.

## Implications for the bot

- Position-level P&L on the dashboard remains aggregate.
- Tax-lot-aware sells are not yet implemented; if/when Plan v4 adds a
  tax-aware exit overlay, this policy will gate which lot the kernel
  prefers to close.

---
**Operator sign-off:**
- Election:
- Effective date:
- Date signed:
