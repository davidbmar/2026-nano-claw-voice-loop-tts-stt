// Canvas 2D offers only affine transforms, and the screen quad is not a
// parallelogram (TL->TR is (490,-22) while BL->BR is (479,-29)), so an affine
// approximation drifts by ~10px at the far corner. A CSS matrix3d gives a true
// projective map, composited on the GPU, at no per-frame cost.

/** Solve A x = b by Gaussian elimination with partial pivoting. */
function solve(A, b) {
  const n = b.length;
  const M = A.map((row, i) => [...row, b[i]]);
  for (let col = 0; col < n; col++) {
    let piv = col;
    for (let r = col + 1; r < n; r++) {
      if (Math.abs(M[r][col]) > Math.abs(M[piv][col])) piv = r;
    }
    if (Math.abs(M[piv][col]) < 1e-12) throw new Error('degenerate quad: singular matrix');
    [M[col], M[piv]] = [M[piv], M[col]];
    const d = M[col][col];
    for (let c = col; c <= n; c++) M[col][c] /= d;
    for (let r = 0; r < n; r++) {
      if (r === col) continue;
      const f = M[r][col];
      if (!f) continue;
      for (let c = col; c <= n; c++) M[r][c] -= f * M[col][c];
    }
  }
  return M.map((row) => row[n]);
}

/**
 * Returns [a,b,c,d,e,f,g,h,1] such that
 *   x' = (a x + b y + c) / (g x + h y + 1)
 *   y' = (d x + e y + f) / (g x + h y + 1)
 * `src` and `dst` are four [x,y] points in tl, tr, br, bl order.
 */
export function solveHomography(src, dst) {
  const A = [];
  const rhs = [];
  for (let i = 0; i < 4; i++) {
    const [x, y] = src[i];
    const [u, v] = dst[i];
    A.push([x, y, 1, 0, 0, 0, -u * x, -u * y]);
    rhs.push(u);
    A.push([0, 0, 0, x, y, 1, -v * x, -v * y]);
    rhs.push(v);
  }
  return [...solve(A, rhs), 1];
}

export function applyHomography(h, [x, y]) {
  const w = h[6] * x + h[7] * y + h[8];
  return [(h[0] * x + h[1] * y + h[2]) / w, (h[3] * x + h[4] * y + h[5]) / w];
}

/** CSS matrix3d is column-major. */
export function cssMatrix3d(h) {
  const [a, b, c, d, e, f, g, i] = h;
  const m = [a, d, 0, g, b, e, 0, i, 0, 0, 1, 0, c, f, 0, 1];
  return `matrix3d(${m.join(', ')})`;
}
