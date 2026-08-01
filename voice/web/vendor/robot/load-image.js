/**
 * Load an <img> and settle deterministically — resolve when it is usable,
 * reject when it is not.
 *
 * This exists because the obvious version has a hang in it.
 *
 * The first attempt was:
 *
 *     await img.decode().catch(
 *       () => new Promise((res) => img.addEventListener('load', res, { once: true })),
 *     );
 *
 * The `.catch` was written for browsers that lack `decode()`. But `decode()`
 * ALSO rejects when the image failed to load — and a broken image never fires
 * `load`, it fires `error`. So a missing asset did not fail the boot, it HUNG
 * it: nothing rejected, so the caller's own error handler never ran, and the
 * page sat blank forever with no console output. A silent hang is far worse
 * than a crash; a crash at least tells you where to look.
 *
 * Two details worth knowing:
 *
 *   - `img.complete` means "no longer in flight", NOT "loaded successfully".
 *     A 404'd image reports `complete === true` with `naturalWidth === 0`.
 *     That pair is the canonical failed-image signature.
 *   - A cached image can finish between setting `src` and the next microtask,
 *     so the `complete` check after assignment is not redundant with the
 *     listeners — it is the path a warm reload actually takes.
 */

/**
 * @param img     an HTMLImageElement (or anything with the same event surface)
 * @param src     the URL to load
 * @param srcset  optional responsive candidates
 * @param sizes   optional layout-size hint, required for `w` descriptors
 * @returns the same element, once it has decoded
 * @throws if the image fails to load
 */
export function loadImage(img, src, { srcset = null, sizes = null } = {}) {
  if (!img) throw new Error('loadImage: no element');
  if (!src) throw new Error('loadImage: no src');

  const settled = new Promise((resolve, reject) => {
    const onLoad = () => { cleanup(); resolve(img); };
    const onError = () => {
      cleanup();
      // currentSrc names the candidate the browser actually chose, which is the
      // one that is missing — `src` alone would misreport it under a srcset.
      reject(new Error(`loadImage: failed to load ${img.currentSrc || src}`));
    };
    function cleanup() {
      img.removeEventListener?.('load', onLoad);
      img.removeEventListener?.('error', onError);
    }

    // Listeners first: assigning src can complete synchronously from cache.
    img.addEventListener('load', onLoad, { once: true });
    img.addEventListener('error', onError, { once: true });

    // srcset must be set before src so the browser selects from the full
    // candidate list rather than committing to src and re-selecting.
    if (srcset) img.srcset = srcset;
    if (sizes) img.sizes = sizes;
    img.decoding = 'async';
    img.src = src;

    if (img.complete) (img.naturalWidth > 0 ? onLoad() : onError());
  });

  return settled.then(async (loaded) => {
    // Now that we KNOW it loaded, decode() is purely an optimisation: it moves
    // the decode off the first paint. Swallowing its rejection is safe here in
    // a way it was not before, because failure is already ruled out.
    await loaded.decode?.().catch(() => {});
    return loaded;
  });
}
