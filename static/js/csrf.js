(function attachCsrfFetchGuard() {
  if (typeof window === "undefined" || typeof window.fetch !== "function") return;

  const token = document
    .querySelector('meta[name="csrf-token"]')
    ?.getAttribute("content");
  if (!token) return;

  const SAFE_METHODS = new Set(["GET", "HEAD", "OPTIONS", "TRACE"]);
  const nativeFetch = window.fetch.bind(window);

  function isSameOriginUrl(input) {
    try {
      const url =
        input instanceof Request
          ? new URL(input.url, window.location.href)
          : new URL(String(input), window.location.href);
      return url.origin === window.location.origin;
    } catch {
      return false;
    }
  }

  window.fetch = function patchedFetch(input, init = {}) {
    const request = input instanceof Request ? input : null;
    const method = (
      init.method ||
      (request ? request.method : "GET") ||
      "GET"
    ).toUpperCase();

    if (!isSameOriginUrl(input) || SAFE_METHODS.has(method)) {
      return nativeFetch(input, init);
    }

    const headers = new Headers(init.headers || (request ? request.headers : {}));
    if (!headers.has("X-CSRFToken") && !headers.has("X-CSRF-Token")) {
      headers.set("X-CSRFToken", token);
    }

    const options = {
      ...init,
      method,
      headers,
    };
    if (!Object.prototype.hasOwnProperty.call(options, "credentials")) {
      options.credentials = request ? request.credentials : "same-origin";
    }

    return nativeFetch(input, options);
  };
})();
