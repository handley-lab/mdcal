"""One-shot: move an mdcal deck's cards into their GTD audience deck.

Deck unification: decks are audiences, so the calendar deck family folds into
the GTD family. Every card in the source deck is recreated in the destination
verbatim — id, title, summary, tags, body, relpath, and every YAML key — with
exactly one transformation: the storage field ``status`` becomes
``event_status`` (deleted, never shadowed). ``dispatched``, ``gcal_*``,
``hidden_occurrences``, ``hidden_from``, and ``mdcal/hidden`` tags ride along
untouched because nothing but the rename is touched.

Preflights every id and relpath against the destination and fails before
writing anything (mddb would refuse anyway; the preflight keeps the tree
clean). Blob cards would need carrying — mdcal decks have none, so any blob is
an abort, not a silent drop. One editor commit, pinned to the destination HEAD
captured at the read. The source deck is left untouched (retired later).

Usage: python unify_decks.py [--dry-run] <src-mdcal-deck> <dst-gtd-deck>
"""

import argparse

import mddb

MDCAL_STATUS = {"CONFIRMED", "TENTATIVE", "CANCELLED"}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("src_deck")
    parser.add_argument("dst_deck")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    src = mddb.MDDB(args.src_deck)
    dst = mddb.MDDB(args.dst_deck)
    dst_base = dst.head()

    entries = src.conn.execute("SELECT id, relpath FROM entries").fetchall()
    dst_ids = {row[0] for row in dst.conn.execute("SELECT id FROM entries")}
    dst_relpaths = {row[0] for row in dst.conn.execute("SELECT relpath FROM entries")}

    id_hits = [cid for cid, _ in entries if cid in dst_ids]
    relpath_hits = [rp for _, rp in entries if rp in dst_relpaths]
    assert not id_hits, f"destination already has ids: {id_hits[:5]}..."
    assert not relpath_hits, f"destination already has relpaths: {relpath_hits[:5]}..."

    cards = []
    renamed = 0
    for cid, relpath in entries:
        card = src.read(cid)
        assert card.blob is None, f"unexpected blob on {cid} ({relpath})"
        yaml = dict(card.yaml)
        status = yaml.get("status")
        if status in MDCAL_STATUS:
            yaml["event_status"] = yaml.pop("status")
            renamed += 1
        cards.append((card, yaml, relpath))

    print(
        f"{args.src_deck} -> {args.dst_deck}: {len(cards)} cards, "
        f"{renamed} status renames"
    )
    if args.dry_run or not cards:
        return
    rationale = f"unify: absorb {args.src_deck} ({len(cards)} cards)"
    with dst.editor(rationale=rationale, base=dst_base) as editor:
        for card, yaml, relpath in cards:
            editor.create(
                title=card.title,
                summary=card.summary,
                tags=card.yaml.get("tags") or None,
                body=card.body,
                relpath=relpath,
                yaml=yaml,
            )

    moved = dst.conn.execute(
        f"SELECT count(*) FROM entries WHERE id IN ({','.join('?' * len(entries))})",
        [cid for cid, _ in entries],
    ).fetchone()[0]
    old_status = dst.conn.execute(
        "SELECT count(*) FROM entry_fields "
        "WHERE key = 'status' AND value_str IN ('CONFIRMED', 'TENTATIVE', 'CANCELLED')"
    ).fetchone()[0]
    missing = dst.conn.execute(
        "SELECT count(*) FROM entries e "
        "JOIN entry_fields ds ON ds.entry_rowid = e.rowid AND ds.key = 'dtstart_epoch' "
        "JOIN entry_fields de ON de.entry_rowid = e.rowid AND de.key = 'dtend_epoch' "
        "JOIN entry_fields u ON u.entry_rowid = e.rowid AND u.key = 'uid' "
        "JOIN entry_fields s ON s.entry_rowid = e.rowid AND s.key = 'source' "
        "WHERE NOT EXISTS (SELECT 1 FROM entry_fields es "
        "WHERE es.entry_rowid = e.rowid AND es.key = 'event_status')"
    ).fetchone()[0]
    print(
        f"moved {moved}/{len(entries)}; old status remaining = {old_status}; "
        f"event-shaped missing event_status = {missing}"
    )
    assert moved == len(entries) and old_status == 0 and missing == 0


if __name__ == "__main__":
    main()
