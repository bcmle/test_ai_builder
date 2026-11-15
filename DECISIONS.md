# Five prototype decisions

1. **A fixed collection of 20 distinct business models.** The CEO originally asked
   for 10–15 inputs; the project owner expanded this to 20. Freeze membership before
   filtering, keep every outcome visible, and never replace rejected models with
   better-looking results. Companies are examples, not additional business models.

2. **Comparable launch assumptions and explicit evidence gaps.** Evaluate a
   two-person US launch with $15,000 excluding salaries and a 90-day first-payment
   goal. State-specific obligations remain unresolved where a state is needed.
   An unresolved mandatory criterion prevents final ranking; a score cannot
   compensate for it. Show the source, date, method and qualitative confidence.

3. **Independent review before a published score.** Critic and Verifier receive
   separate, allowlisted inputs. Verifier never receives Critic confidence,
   priorities or business conclusions. Check score premises separately. Weights
   stay in configuration: customer access 25%, willingness to pay 25%, test speed
   20%, payment cycle 20%, resources 10%. The result is a heuristic, not a calibrated
   probability; compare conservative intervals within feasibility groups.

4. **One sequential server process and a shared budget.** Reuse LangGraph,
   PostgreSQL receipts/checkpoints and the current provider adapters. All discovery
   and model runs share a $100 collection cap. Reuse finished work when resuming.
   A permanent worker, global catalogue and owner dashboard would delay this
   prototype without improving the CEO's first decision.

5. **A read-only website built from a public projection.** FastAPI/Jinja and small
   local CSS/JavaScript keep the UI simple to deploy. The website exposes only
   approved fields and short evidence excerpts; private source documents, provider
   payloads and secrets are not served. HTTP by server IP is an explicitly accepted
   temporary prototype choice. Add a domain, HTTPS and authentication before any
   owner controls are exposed.
