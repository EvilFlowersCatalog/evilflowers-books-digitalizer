/**
 * OpenCV.js (WASM, ~10 MB) for instant crop / deskew / threshold previews.
 *
 * Loaded with a dynamic import on first use, so it is a separate chunk that only
 * operators who open the page-properties preview ever download. Typed with the
 * few calls we make instead of the package's very large declaration set.
 */

interface Mat {
  cols: number;
  rows: number;
  roi(rect: unknown): Mat;
  delete(): void;
}

interface Cv {
  Mat: new () => Mat;
  Size: new (w: number, h: number) => unknown;
  Point: new (x: number, y: number) => unknown;
  Rect: new (x: number, y: number, w: number, h: number) => unknown;
  Scalar: new (...v: number[]) => unknown;
  imread(source: HTMLCanvasElement | HTMLImageElement): Mat;
  imshow(target: HTMLCanvasElement, mat: Mat): void;
  getRotationMatrix2D(center: unknown, angle: number, scale: number): Mat;
  warpAffine(src: Mat, dst: Mat, m: Mat, size: unknown, flags: number, border: number, value: unknown): void;
  cvtColor(src: Mat, dst: Mat, code: number): void;
  threshold(src: Mat, dst: Mat, thresh: number, max: number, type: number): number;
  INTER_LINEAR: number;
  BORDER_CONSTANT: number;
  COLOR_RGBA2GRAY: number;
  THRESH_BINARY: number;
  THRESH_OTSU: number;
  onRuntimeInitialized?: () => void;
}

/**
 * Boxed because the Emscripten module object is a *thenable* whose `then` resolves
 * to itself: returning or awaiting it directly makes promise adoption recurse
 * forever and freezes the tab. Never `await` / `resolve()` the bare module.
 */
export interface CvBox {
  cv: Cv;
}

let loading: Promise<CvBox> | null = null;

function ready(cv: Cv): boolean {
  return typeof cv.Mat === "function" && typeof cv.imread === "function";
}

/**
 * Loaded as a classic <script> (the UMD build sets `window.cv`), not via `import()`:
 * bundler CJS interop copies the module's own keys, `then` included, onto the
 * namespace object, and awaiting that namespace hangs the same way.
 */
function injectScript(src: string): Promise<void> {
  return new Promise((resolve, reject) => {
    const el = document.createElement("script");
    el.src = src;
    el.async = true;
    el.onload = () => resolve();
    el.onerror = () => reject(new Error("could not load OpenCV.js"));
    document.head.appendChild(el);
  });
}

export function loadOpenCv(): Promise<CvBox> {
  loading ??= (async () => {
    const { default: url } = await import("@techstark/opencv-js/dist/opencv.js?url");
    await injectScript(url);
    const cv = (window as unknown as { cv?: Cv }).cv;
    if (!cv) throw new Error("OpenCV.js did not register window.cv");
    if (!ready(cv)) {
      await new Promise<void>((resolve) => {
        cv.onRuntimeInitialized = () => resolve();
      });
    }
    return { cv };
  })();
  loading.catch(() => {
    loading = null; // allow a retry after a network error
  });
  return loading;
}

export interface PreviewParams {
  /** crop in *image* pixels of the preview source: [x0, y0, x1, y1] */
  crop: [number, number, number, number] | null;
  deskewDeg: number;
  /** null = colour; "otsu" = automatic; number = fixed 0..255 */
  threshold: number | "otsu" | null;
}

/** Deskew → crop → threshold, the order the cleaner applies them. Returns the used threshold. */
export function renderPreview(cv: Cv, source: HTMLImageElement, target: HTMLCanvasElement, p: PreviewParams): number | null {
  const owned: Mat[] = [];
  const track = (m: Mat) => (owned.push(m), m);
  try {
    let mat = track(cv.imread(source));
    if (p.deskewDeg) {
      const rot = track(cv.getRotationMatrix2D(new cv.Point(mat.cols / 2, mat.rows / 2), p.deskewDeg, 1));
      const out = track(new cv.Mat());
      cv.warpAffine(mat, out, rot, new cv.Size(mat.cols, mat.rows), cv.INTER_LINEAR, cv.BORDER_CONSTANT, new cv.Scalar(255, 255, 255, 255));
      mat = out;
    }
    if (p.crop) {
      const [x0, y0, x1, y1] = p.crop;
      const x = Math.max(0, Math.min(mat.cols - 1, Math.round(x0)));
      const y = Math.max(0, Math.min(mat.rows - 1, Math.round(y0)));
      const w = Math.max(1, Math.min(mat.cols - x, Math.round(x1 - x0)));
      const h = Math.max(1, Math.min(mat.rows - y, Math.round(y1 - y0)));
      mat = track(mat.roi(new cv.Rect(x, y, w, h)));
    }
    let used: number | null = null;
    if (p.threshold !== null) {
      const gray = track(new cv.Mat());
      cv.cvtColor(mat, gray, cv.COLOR_RGBA2GRAY);
      const bw = track(new cv.Mat());
      const fixed = p.threshold === "otsu" ? null : p.threshold;
      used = cv.threshold(gray, bw, fixed ?? 0, 255, cv.THRESH_BINARY | (fixed === null ? cv.THRESH_OTSU : 0));
      mat = bw;
    }
    cv.imshow(target, mat);
    return used;
  } finally {
    owned.forEach((m) => m.delete());
  }
}
