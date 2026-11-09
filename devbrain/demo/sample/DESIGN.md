# Checkout requirements

All prices and totals must use integer cents. Floating-point money is forbidden.

The checkout must add all item prices exactly, return zero for an empty cart,
and reject negative values, floats and booleans.

Before considering the checkout verified, run the `unit` check profile against
the current code. A passing result for an earlier revision is not current proof.
