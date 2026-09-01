# Notes

One page is plenty. Four short sections:

## Decisions

What you decided and why — especially what your endpoint does when the ECB
published no rate for the date that was asked for.

## With another day

What you would have built or fixed next.

## AI tools

I planned this case through **Traycer**, with **Opus 5** as the planning model.

Traycer is a harness I have been trying out recently, which is why it is here. It keeps the
plan outside the chat: the technical decisions and the tickets are written artifacts I read
and corrected before any code existed. During implementation I am then checking code against
a plan I already approved, instead of judging generated code on the spot — which is why I can
say I know what is in this repo.

I did not delegate the upstream research: I sent real requests to `api.frankfurter.dev` first,
so the plan is built on observed behaviour rather than assumptions.

## One thing the AI got wrong

Something concrete, in this task: how you noticed, and what you changed. If it
got nothing wrong, say so and tell us what you verified instead.
