# Working correspondences

This folder holds SETT's working correspondences with external instruments:
one JSON record per instrument, the page generated from each record, and a
generated `index.md`. The records state what SETT's mechanisms do for
selected provisions and the tests that show it, and name what stays with the
deployment. They are not compliance statements.

The format is internal and experimental; see
[Working correspondences](../../docs/working_correspondences.md). SETT never
reads these files.

Edit a record, never a page, then run from the project root:

```bash
python tools/correspondence.py validate
python tools/correspondence.py generate
python tools/correspondence.py check
```

A record reaches this folder as a candidate. It becomes approved only when
the project owner approves its exact revision; any later change makes it a
candidate again.
