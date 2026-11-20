export function sum(values) {
  return values.reduce((acc, n) => acc + n, 0);
}

export function average(values) {
  if (!Array.isArray(values) || values.length === 0) {
    throw new Error("Cannot compute average of empty array");
  }
  return sum(values) / values.length;
}
