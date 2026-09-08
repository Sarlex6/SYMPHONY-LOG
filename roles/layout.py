"""Dynamic row and category management for the PERSONNEL sheet.

Category boundaries are not fixed. HIGH-RANK occupies rows 10..n, MID-RANK the
rows after that, LOW-RANK the rest — and all three move whenever someone's rank
changes. This module computes where every record *should* sit and the minimal
set of row operations to get there.

Deliberately NOT a blind sort of the sheet: columns A, B, I, M are design-only
and must keep their physical rows. So a re-layout writes managed *values* into
managed *rows* in the target order, which leaves every design cell, every
conditional format and every data validation rule attached to its original row.

Sort order within a category:
    1. normal ranks, by configured rank order, descending (most senior first)
    2. special ranks, after all normal ranks, grouped by special-rank type in
       configured order — all Overseers together, then all Ambassadors, never
       interleaved
    3. stable tiebreak on entry date then record uid, so equal-ranked people do
       not shuffle between runs
"""

from roles import columns
from roles.models import Category, Status, parse_entry_date


# ── Ordering ─────────────────────────────────────────────────────────────────

def effective_category(record, cfg):
    """The category a record belongs in, derived from its rank.

    Rank is authoritative over the category cell: column C is a rendering of the
    rank's configured category, so a rank change moves the person automatically.
    Falls back to whatever column C says when the rank is unknown, so records
    with a not-yet-configured rank are not swept into the wrong section.
    """
    rank = cfg.rank(record.rank_key)
    if rank and rank.category:
        return rank.category
    return record.category


def sort_key(record, cfg):
    """Sort key within a category. Lower tuple sorts higher on the sheet."""
    rank = cfg.rank(record.rank_key)

    if rank is None:
        # Unknown rank: park at the bottom of its section rather than guessing a
        # position that would silently reorder real people around it.
        return (2, 0, 0, "", record.record_uid)

    if rank.special:
        # Special ranks come after all normal ranks, grouped by type.
        group = (1, rank.special_group_order, -rank.order)
    else:
        group = (0, 0, -rank.order)

    entry = parse_entry_date(record.entry_date)
    entry_key = entry.strftime("%Y%m%d") if entry else "99999999"

    return group + (entry_key, record.record_uid)


def order_records(records, cfg):
    """Full sheet order: category sections in configured order, sorted within each.

    Records whose category cannot be resolved are appended last so they are
    visible and fixable rather than dropped.
    """
    by_category = {category: [] for category in cfg.category_order}
    orphans = []

    for record in records:
        if record.is_empty_row():
            continue
        category = effective_category(record, cfg)
        if category in by_category:
            by_category[category].append(record)
        else:
            orphans.append(record)

    ordered = []
    for category in cfg.category_order:
        section = sorted(by_category[category], key=lambda r: sort_key(r, cfg))
        ordered.extend(section)

    ordered.extend(sorted(orphans, key=lambda r: sort_key(r, cfg)))
    return ordered


def category_boundaries(ordered_records, cfg, first_row=None):
    """{Category: (first_row, last_row)} for the current ordering.

    Empty sections get (row, row - 1), an empty range, rather than being absent.
    """
    first_row = first_row or columns.FIRST_MANAGED_ROW
    boundaries = {}
    cursor = first_row

    for category in cfg.category_order:
        count = sum(
            1 for r in ordered_records if effective_category(r, cfg) is category
        )
        boundaries[category] = (cursor, cursor + count - 1)
        cursor += count

    return boundaries


# ── Layout planning ──────────────────────────────────────────────────────────

class LayoutPlan:
    """The difference between where records are and where they should be."""

    def __init__(self, ordered_records, footer_row):
        self.ordered_records = ordered_records
        self.footer_row = footer_row
        self.rows_to_insert = 0
        self.rows_to_delete = 0
        #: [(record, target_row)] for records that need to move.
        self.moves = []
        #: Rows that will be left over and must be blanked.
        self.rows_to_clear = []

    @property
    def needed_rows(self):
        return len(self.ordered_records)

    @property
    def changed(self):
        return bool(self.moves or self.rows_to_insert or self.rows_to_delete
                    or self.rows_to_clear)

    def summary(self):
        return (
            f"{self.needed_rows} record(s), {len(self.moves)} move(s), "
            f"+{self.rows_to_insert}/-{self.rows_to_delete} row(s), "
            f"{len(self.rows_to_clear)} row(s) to clear"
        )


def plan_layout(records, cfg, footer_row):
    """Compute the row operations needed to bring the sheet to its target state.

    Pure: reads nothing, writes nothing. The repository executes the plan.
    """
    ordered = order_records(records, cfg)
    plan = LayoutPlan(ordered, footer_row)

    capacity = columns.managed_row_count(footer_row)
    needed = len(ordered) + columns.SLACK_ROWS

    if needed > capacity:
        plan.rows_to_insert = needed - capacity
    elif capacity > needed:
        # Only surrender rows that hold nothing real. An EMPTY-status row is
        # exactly the case this is here to clean up.
        plan.rows_to_delete = capacity - needed

    final_footer = footer_row + plan.rows_to_insert - plan.rows_to_delete
    final_capacity = columns.managed_row_count(final_footer)

    for index, record in enumerate(ordered):
        target_row = columns.FIRST_MANAGED_ROW + index
        if record.row != target_row:
            plan.moves.append((record, target_row))

    first_unused = columns.FIRST_MANAGED_ROW + len(ordered)
    last_row = columns.FIRST_MANAGED_ROW + final_capacity - 1
    if first_unused <= last_row:
        plan.rows_to_clear = list(range(first_unused, last_row + 1))

    return plan


# ── Placement for a single new record ───────────────────────────────────────

def insertion_index(record, existing_records, cfg):
    """Index in the ordered list where a new record belongs.

    Used by registration to report where the person landed. The actual write
    still goes through a full layout pass, which is cheap (one batched update)
    and cannot drift out of sync with this function.
    """
    ordered = order_records(existing_records, cfg)
    target_category = effective_category(record, cfg)

    try:
        category_rank = cfg.category_order.index(target_category)
    except ValueError:
        return len(ordered)

    key = sort_key(record, cfg)

    for index, other in enumerate(ordered):
        other_category = effective_category(other, cfg)
        try:
            other_category_rank = cfg.category_order.index(other_category)
        except ValueError:
            return index

        if other_category_rank > category_rank:
            return index
        if other_category_rank == category_rank and key < sort_key(other, cfg):
            return index

    return len(ordered)


def describe_placement(record, records, cfg):
    """'HIGH-RANK, row 14 of 16' — for the response to /register and /set rank."""
    ordered = order_records(records, cfg)
    category = effective_category(record, cfg)
    boundaries = category_boundaries(ordered, cfg)

    for index, other in enumerate(ordered):
        if other.record_uid == record.record_uid:
            row = columns.FIRST_MANAGED_ROW + index
            first, last = boundaries.get(category, (row, row))
            position = row - first + 1
            total = max(0, last - first + 1)
            label = category.value if category else "unassigned"
            return f"{label}, row {row} (position {position} of {total})"

    return "not yet placed"


# ── Integrity checks ─────────────────────────────────────────────────────────

def find_misplaced(records, cfg):
    """Records sitting in the wrong category section, for diagnostics."""
    misplaced = []
    for record in records:
        if record.is_empty_row():
            continue
        expected = effective_category(record, cfg)
        if expected and record.category and expected is not record.category:
            misplaced.append((record, record.category, expected))
    return misplaced


def find_empty_rows(records):
    """Managed rows holding no real record — candidates for removal."""
    return [r for r in records if r.is_empty_row()]


def find_duplicates(records):
    """Records sharing a Discord ID or Roblox ID.

    Never auto-resolved: which duplicate is correct is a human judgement, and
    the sheet is authoritative. Surfaced for an operator to fix.
    """
    by_discord = {}
    by_roblox = {}
    duplicates = []

    for record in records:
        if record.is_empty_row():
            continue
        if record.discord_id:
            by_discord.setdefault(record.discord_id, []).append(record)
        if record.roblox_id:
            by_roblox.setdefault(record.roblox_id, []).append(record)

    for discord_id, group in by_discord.items():
        if len(group) > 1:
            duplicates.append(("discord_id", discord_id, group))
    for roblox_id, group in by_roblox.items():
        if len(group) > 1:
            duplicates.append(("roblox_id", roblox_id, group))

    return duplicates


def status_is_placeholder(status):
    """EMPTY marks sheet maintenance, not a person."""
    return status is Status.EMPTY


def unknown_categories(records, cfg):
    """Category values present on the sheet that the configuration does not know."""
    known = set(cfg.category_order)
    found = set()
    for record in records:
        if record.category and record.category not in known:
            found.add(record.category)
    return found


def unknown_ranks(records, cfg):
    """Rank strings on the sheet with no matching configuration entry.

    Expected to be everything until the rank definitions are supplied; useful
    afterwards for catching typos in manual edits.
    """
    unknown = {}
    for record in records:
        if record.is_empty_row() or not record.rank_key:
            continue
        if cfg.rank(record.rank_key) is None:
            unknown.setdefault(record.rank_key, []).append(record)
    return unknown


def _category_label(category):
    return category.value if isinstance(category, Category) else str(category or "")
