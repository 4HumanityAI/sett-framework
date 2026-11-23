# Working correspondences

A working correspondence says, for selected provisions of an
external instrument, what SETT's mechanisms do and which tests show it, and
names what stays with the deployment. It is not a compliance statement.

## What they are

One record per instrument, and a page generated from it.

The records live in `governance/mappings/`, next to the pages generated from
them. `tools/correspondence.py` validates the records and generates the
pages; it uses the standard library only. Neither the records nor the tool
are part of the `sett` package, and SETT never reads them.

The format, `sett.working_correspondence` version 1, is internal and
experimental. It promises no compatibility; a stable public schema is not
part of this version.

## What a record holds

Identities, locators, approved statements, mechanisms with
their evidence, and what stays with the deployment.

| Part | Content |
|---|---|
| Instrument | Title, publisher, captured edition and identifiers, the captured copy's SHA-256, and whether the edition was checked against the publisher's current one |
| Requirement | Identifier, interpretation revision, locator in the instrument, and the approved interpretation, criterion and limits, sealed by `approved_sha256` |
| Capability | Each SETT mechanism that contributes, a file of this project that describes it, and the tests that show it; an empty list means SETT provides none |
| Fixed dimensions | `applicability` and `configuration` are always `deployment_decides`; `institutional_outcome` is always `not_assessed_by_sett` |
| What stays with the deployment | The obligations SETT does not carry |
| Comparisons | Pairs of requirements that share a mechanism or a theme, always `unresolved` |
| Approval | `pending`, or `approved` with the revision, the record's digest and the date |

A record holds no source text and no paraphrase: its locators point to the
instrument itself.

## What a record never says

No applicability, no satisfaction, no score.

Whether an instrument applies, how a deployment is configured and whether an
institution's requirement is met are the deployment's to decide, and SETT
cannot know them. The fixed dimensions say so, and the validator refuses any
other value, any score, percentage or overall verdict, and any field the
format does not define. One mechanism can support two requirements without
making them equivalent: a comparison records that, and decides nothing.

## Approval and revisions

An approval covers one exact revision.

- A candidate may be incomplete.
- An approved record must be complete: every interpretation sealed, the
  edition checked, and what stays with the deployment stated. Its approval
  names its revision and the digest of everything but the approval itself.
- Any change, including a newer edition of the instrument, makes the record
  invalid as approved until it is approved again. The earlier revision stays
  valid as history.
- Evidence must name tests that exist in this project, and every page must
  equal what its record generates.

## Commands

Validate, generate and check, from the project root.

| Command | Effect |
|---|---|
| `python tools/correspondence.py validate [FOLDER]` | Reports every problem of every record |
| `python tools/correspondence.py generate [FOLDER]` | Writes each record's page and `index.md`; refuses while a record is invalid |
| `python tools/correspondence.py check [FOLDER]` | Reports a page that differs from its record, or that no record generates |

The default folder is `governance/mappings`. Each command returns 0 when
everything is in order and 1 otherwise.
