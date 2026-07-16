"""One-shot: delete the stale pre-cutover event snapshot from the GTD personal deck.

The GTD personal deck's root carries a fossil of the mk2 migration: a full
snapshot of the live mdcal personal deck's event cards (same ``source + uid +
recurrence_id`` identities, different mddb ids, pre-gcal_id renders).
This deletes exactly that snapshot, nothing else, before the live cards move
in during deck unification.

The selector requires the FULL old mdcal event identity — root-level relpath,
``uid`` + ``source`` + both epochs present, old ``status`` in the iCalendar
value set — AND an identity match to a live mdcal-deck card with a differing
id. Root-level event-shaped cards failing any conjunct are left untouched and
listed; snapshot cards whose live counterpart was itself deleted upstream are
deleted only under ``--tombstoned``, after eyeballing the list.

Snapshot cards DO carry ``area/*`` tags (measured: ~7,000 of 9,816, of which
697 exceeded their live counterpart's tags). Run
``transfer_snapshot_tags.py`` first; the pre-deletion assertions here then
prove no selected card still carries a tag its live counterpart lacks, and
that none carries ``otter_ids``, GTD ``status`` values, or hide annotations.
Any hit aborts before the destructive pass. One editor commit, pinned to the
HEAD captured at the read.

Usage: python delete_stale_snapshot.py [--dry-run] [--tombstoned] <gtd-personal-deck> <live-mdcal-personal-deck>
"""

import argparse

import mddb

MDCAL_STATUS = {"CONFIRMED", "TENTATIVE", "CANCELLED"}

IDENTITY_SQL = (
    "SELECT e.id, e.relpath, u.value_str, s.value_str, "
    "COALESCE(r.value_str, ''), st.value_str "
    "FROM entries e "
    "JOIN entry_fields u ON u.entry_rowid = e.rowid AND u.key = 'uid' "
    "JOIN entry_fields s ON s.entry_rowid = e.rowid AND s.key = 'source' "
    "JOIN entry_fields ds ON ds.entry_rowid = e.rowid AND ds.key = 'dtstart_epoch' "
    "JOIN entry_fields de ON de.entry_rowid = e.rowid AND de.key = 'dtend_epoch' "
    "LEFT JOIN entry_fields r ON r.entry_rowid = e.rowid AND r.key = 'recurrence_id' "
    "JOIN entry_fields st ON st.entry_rowid = e.rowid AND st.key = {status_key!r}"
)


def _identities(db, status_key):
    rows = db.conn.execute(IDENTITY_SQL.format(status_key=status_key)).fetchall()
    return {
        (source, uid, rid): (cid, relpath, status)
        for cid, relpath, uid, source, rid, status in rows
    }


def _field_sets(db, key, ids):
    placeholders = ",".join("?" * len(ids))
    out = {}
    for cid, value in db.conn.execute(
        f"SELECT e.id, f.value_str FROM entries e "
        f"JOIN entry_fields f ON f.entry_rowid = e.rowid AND f.key = ? "
        f"WHERE e.id IN ({placeholders})",
        (key, *ids),
    ):
        out.setdefault(cid, set()).add(value)
    return out


def _assert_deletable(gtd, stale, live_tags_by_stale_cid):
    marks = {}
    for key in ("otter_ids", "hidden_occurrences", "hidden_from", "status"):
        for cid, values in _field_sets(gtd, key, stale).items():
            bad = values - MDCAL_STATUS if key == "status" else values
            if bad:
                marks.setdefault(cid, []).append(f"{key}={sorted(bad)}")
    for cid, tags in _field_sets(gtd, "tags", stale).items():
        untransferred = tags - live_tags_by_stale_cid.get(cid, set())
        if untransferred:
            marks.setdefault(cid, []).append(
                f"tags not on live={sorted(untransferred)}"
            )
    assert not marks, f"selected stale cards not safe to delete: {marks}"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("gtd_deck")
    parser.add_argument("live_deck")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--tombstoned",
        action="store_true",
        help="also delete snapshot cards whose live counterpart was itself "
        "deleted upstream since the snapshot (full old-event identity, no live "
        "match) — run only after eyeballing the listed cards",
    )
    args = parser.parse_args()

    gtd = mddb.MDDB(args.gtd_deck)
    base = gtd.head()
    live = mddb.MDDB(args.live_deck)
    live_by_identity = _identities(live, "event_status")
    if not live_by_identity:
        live_by_identity = _identities(live, "status")

    stale, tombstoned, left, by_source = [], [], [], {}
    live_cid_by_stale = {}
    for identity, (cid, relpath, status) in _identities(gtd, "status").items():
        if "/" in relpath or status not in MDCAL_STATUS:
            left.append((cid, relpath, "shape"))
            continue
        live_hit = live_by_identity.get(identity)
        if live_hit is None:
            if args.tombstoned:
                tombstoned.append(cid)
                by_source[identity[0]] = by_source.get(identity[0], 0) + 1
            else:
                left.append((cid, relpath, "no live counterpart"))
            continue
        if live_hit[0] == cid:
            left.append((cid, relpath, "same id as live card"))
            continue
        stale.append(cid)
        live_cid_by_stale[cid] = live_hit[0]
        by_source[identity[0]] = by_source.get(identity[0], 0) + 1

    print(
        f"{args.gtd_deck}: {len(stale)} stale snapshot cards selected"
        f" + {len(tombstoned)} tombstoned"
    )
    for source, count in sorted(by_source.items()):
        print(f"  source {source}: {count}")
    for cid, relpath, reason in left:
        print(f"  left untouched ({reason}): {relpath} {cid}")

    if stale:
        live_tags = _field_sets(live, "tags", list(live_cid_by_stale.values()))
        live_tags_by_stale = {
            cid: live_tags.get(live_cid, set())
            for cid, live_cid in live_cid_by_stale.items()
        }
        _assert_deletable(gtd, stale, live_tags_by_stale)
    if tombstoned:
        marks = {}
        for key in ("otter_ids", "hidden_occurrences", "hidden_from", "status"):
            for cid, values in _field_sets(gtd, key, tombstoned).items():
                bad = values - MDCAL_STATUS if key == "status" else values
                if bad:
                    marks.setdefault(cid, []).append(f"{key}={sorted(bad)}")
        assert not marks, f"tombstoned cards not safe to delete: {marks}"
        for cid, tags in _field_sets(gtd, "tags", tombstoned).items():
            print(f"  tombstoned {cid} dies with tags {sorted(tags)}")
    doomed = stale + tombstoned
    if args.dry_run or not doomed:
        return
    with gtd.editor(
        rationale=f"delete stale mdcal snapshot ({len(doomed)} cards)", base=base
    ) as editor:
        for cid in doomed:
            editor.delete(cid)
    remaining = gtd.conn.execute(
        "SELECT count(*) FROM entry_fields "
        "WHERE key = 'status' AND value_str IN ('CONFIRMED', 'TENTATIVE', 'CANCELLED')"
    ).fetchone()[0]
    print(f"deleted {len(doomed)}; remaining old mdcal status values: {remaining}")
    assert remaining == 0, "stale snapshot deletion incomplete"


if __name__ == "__main__":
    main()
