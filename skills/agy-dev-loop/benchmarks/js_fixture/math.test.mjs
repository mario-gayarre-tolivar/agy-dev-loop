import test from "node:test";
import assert from "node:assert/strict";

import { average, sum } from "./math.mjs";

test("sum computes total of numeric array", () => {
  assert.equal(sum([]), 0);
  assert.equal(sum([1, 2, 3, 4]), 10);
});

test("average computes mean and throws on empty array", () => {
  assert.equal(average([2, 4, 6]), 4);
  assert.throws(() => average([]), /empty array/);
});
