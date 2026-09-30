# Golden fixtures

Every file is one fixture that both SDKs (and `tools/validate_spec.py`) load. A fixture that passes in one language and fails in the other fails CI (Phase 5 parity gate).

```jsonc
{
  "id": "table-one-row",                 // == file name
  "description": "…",
  "kind": "envelope" | "table",          // envelope: input is a full envelope; table: input is a fusion table object
  "input": { … },                        // the JSON as received off the wire
  "schemaValid": true,                   // must spec/envelope.schema.json accept input? (tables validate against #/$defs/table)
  "valid": true,                         // must the SDK decoder accept input? (schema cannot check cells; decoders can)
  "canonical": { … },                    // optional: expected result of decode → encode. Defaults to input.
  "decoded": { … },                      // optional: language-neutral assertions, see below
  "error": "DECODE"                      // present when valid is false
}
```

`decoded` assertions:

- envelope: `kind`, `operation`, `paramKeys` (sorted), `status`, `code`, `bodyIsTable`, `table` (see below)
- table: `nrow`, `ncol`, `names`, `types`

Decoders compare `canonical` **structurally** (numbers by value, so `2` and `2.0` are equal; strings, booleans and null exactly; object key order ignored).

## Cell encoding rules (normative)

| type | JSON cell | Notes |
| :-- | :-- | :-- |
| `string` | string | no coercion from numbers |
| `integer` | number without fraction, \|v\| ≤ 2^53 − 1 | decoders accept a whole-number float leniently (`2.0` → `2`) |
| `integer64` | string of decimal digits with optional sign | for values beyond ±2^53; R without `bit64` decodes to character |
| `number` | number | encoders map NaN/NA/±Inf to `null` |
| `boolean` | `true` / `false` | |
| `date` | `YYYY-MM-DD` | proleptic Gregorian, must be a real date |
| `datetime` | RFC 3339, `T` separator, zone required | encoders emit UTC `Z` with 0 or exactly 3 fractional digits; decoders accept 0–9 digits and any offset, normalising to UTC and truncating to milliseconds |

Every cell may be `null`. `rows[i].length == columns.length`. Column names are unique and non-empty.
