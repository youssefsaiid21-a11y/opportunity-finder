# Opportunity classification

Classify each opportunity for a fictional industrial safety software provider. Use only evidence in the supplied text. Do not infer missing legal eligibility or company capabilities. If eligibility is unclear, return a specific question for human review.

## Output Format

You will receive a JSON array of opportunities. Return a JSON array of the **same length** with one object per opportunity, in the **same order**.

Each object must contain exactly these fields:

```json
{
  "raw_id": "string — copy from input",
  "eligibility": "eligible | ineligible | unclear",
  "category": "conference | innovation_challenge | tender | irrelevant",
  "ineligibility_reason": "string or null — required if ineligible, null otherwise",
  "industries_matched": ["list of matched industries from: safety, construction, oil_gas, logistics, warehousing, manufacturing, energy, AI"],
  "geography_matched": ["list of matched geographies from: Saudi Arabia, UAE, Qatar, Singapore, Germany"],
  "deadline_or_date": "ISO8601 datetime string or null",
  "organizer_or_issuer": "string or null",
  "tender_id": "string or null",
  "budget_if_available": "string or null",
  "is_expired": true | false,
  "lawyer_notes": "1-2 sentence reasoning for your decision",
  "question": "string or null — REQUIRED when eligibility is unclear, null otherwise"
}
```

**CRITICAL RULES:**
- Return ONLY valid JSON — a single array, no markdown, no explanations outside the JSON
- Do not omit any fields
- Do not add extra fields
- The array must have exactly the same number of objects as the input array
- `question` must never be a generic note — always a specific, answerable question
- When `eligibility` is `"unclear"`, `question` must not be null
- ALL output text (lawyer_notes, question, ineligibility_reason) MUST be in English regardless of the source language
- `geography_matched` can include ANY country where the opportunity is located — not limited to primary markets
