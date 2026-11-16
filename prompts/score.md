# Opportunity scoring

Score each opportunity for a fictional industrial safety software provider. Treat all organization details as demonstration assumptions, not a real company profile. Use the scoring dimensions and output contract below.

Maximum weights: geography_match 2.0, industry_match 2.0, use_case_directness 2.5, buyer_relevance 1.5, source_quality 1.0, freshness_and_urgency 1.0. Priority: High 8–10, Medium 5–7, Low 1–4. Assess manufacturing, workplace safety, software fit, evidence quality and deadlines without inventing facts.

## Output Format

You will receive a JSON array of opportunity objects. Return a JSON array of result objects, one per input, in the same order.

Each result object:

```json
{
  "raw_id": "string — copy from input",
  "relevance_score": integer between 1 and 10,
  "priority_label": "High | Medium | Low",
  "scoring_breakdown": {
    "geography_match": float,
    "industry_match": float,
    "use_case_directness": float,
    "buyer_relevance": float,
    "source_quality": float,
    "freshness_and_urgency": float
  }
}
```

**CRITICAL RULES:**
- Return ONLY a valid JSON array — no markdown, no explanations outside the JSON, no extra fields
- The output array MUST have the same length as the input array, in the same order
- `scoring_breakdown` values must not exceed the maximum for each dimension
- `relevance_score` must equal the rounded sum of all `scoring_breakdown` values (range 1-10)
- Do not include any narrative, action, or rationale fields — they are not used downstream
