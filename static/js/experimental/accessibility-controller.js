/**
 * accessibility-controller.js - Keyboard navigation, screen-reader mirror, and motion controls for Market Observatory.
 *
 * Provides full keyboard shortcuts, ARIA live region announcements,
 * an accessible DOM mirror table, and prefers-reduced-motion integration.
 */

(function (global) {
  "use strict";

  class AccessibilityController {
    constructor(state, elements) {
      this.state = state;
      this.els = elements || {};
      this._modalReturnFocusTarget = null;

      this.bindEvents();
      this.bindState();
    }

    bindEvents() {
      // Global keydown shortcuts
      this._keydownHandler = (e) => this.handleKeydown(e);
      window.addEventListener("keydown", this._keydownHandler);

      // Motion toggle button
      if (this.els.motionBtn) {
        this.els.motionBtn.addEventListener("click", () => {
          this.state.toggleReducedMotion();
        });
      }

      // Pause toggle button
      if (this.els.pauseBtn) {
        this.els.pauseBtn.addEventListener("click", () => {
          this.state.togglePause();
        });
      }

      // Help modal toggle button
      if (this.els.helpBtn) {
        this.els.helpBtn.addEventListener("click", () => {
          this.openHelpModal();
        });
      }

      if (this.els.helpCloseBtn) {
        this.els.helpCloseBtn.addEventListener("click", () => {
          this.closeHelpModal();
        });
      }

      // Listen for system prefers-reduced-motion changes
      if (window.matchMedia) {
        this._motionMediaQuery = window.matchMedia(
          "(prefers-reduced-motion: reduce)",
        );
        this._motionListener = (e) => {
          if (localStorage.getItem("mns_observatory_reduced_motion") === null) {
            this.state.set({ reducedMotion: e.matches });
          }
        };
        try {
          this._motionMediaQuery.addEventListener(
            "change",
            this._motionListener,
          );
        } catch (_e) {
          this._motionMediaQuery.addListener(this._motionListener);
        }
      }
    }

    bindState() {
      this.state.subscribe((key, val, data) => {
        if (key === "stocks" || key === "selectedSymbol") {
          this.updateScreenReaderTable(data);
          if (key === "selectedSymbol") {
            this.announce(`基準銘柄が ${val} に設定されました`);
          }
        } else if (key === "paused") {
          this.updatePauseUI(val);
        } else if (key === "reducedMotion") {
          this.updateMotionUI(val);
        }
      });
    }

    destroy() {
      window.removeEventListener("keydown", this._keydownHandler);
      if (this._motionMediaQuery && this._motionListener) {
        try {
          this._motionMediaQuery.removeEventListener(
            "change",
            this._motionListener,
          );
        } catch (_e) {
          this._motionMediaQuery.removeListener(this._motionListener);
        }
      }
    }

    handleKeydown(e) {
      const openModal = this.getOpenModal();
      if (openModal) {
        if (e.key === "Escape") {
          if (openModal === this.els.searchModal) {
            this.closeSearchModal();
          } else if (openModal === this.els.aiDiveOverlay) {
            this.state.closeAiDive();
          } else {
            this.closeHelpModal();
          }
          e.preventDefault();
          return;
        }
        if (e.key === "Tab") {
          this.trapModalFocus(e, openModal);
        }
        // While a modal is open, do not let page-level shortcuts affect
        // controls behind it (including when focus is in a text input).
        return;
      }

      // Don't intercept keyboard shortcuts if active element is an input, textarea, or select
      const tag = document.activeElement
        ? document.activeElement.tagName.toLowerCase()
        : "";
      if (["input", "textarea", "select"].includes(tag)) {
        return;
      }

      switch (e.key) {
        case "Escape":
          if (
            this.els.searchModal &&
            !this.els.searchModal.classList.contains("hidden")
          ) {
            this.closeSearchModal();
            e.preventDefault();
          } else if (this.state.state.aiDiveOpen) {
            this.state.closeAiDive();
            e.preventDefault();
          } else if (
            this.state.state.isConnectMode ||
            this.state.state.connectedSymbols.length > 0
          ) {
            this.state.clearConnectedSymbols();
            this.state.toggleConnectMode(false);
            e.preventDefault();
          } else if (
            this.els.helpModal &&
            !this.els.helpModal.classList.contains("hidden")
          ) {
            this.closeHelpModal();
            e.preventDefault();
          }
          break;

        case "/":
          e.preventDefault();
          this.openSearchModal();
          break;

        case "k":
        case "K":
          if (e.ctrlKey || e.metaKey) {
            e.preventDefault();
            this.openSearchModal();
          }
          break;

        case " ":
          // Space: Pause/Resume animation
          e.preventDefault();
          this.state.togglePause();
          break;

        case "d":
        case "D":
          // Open AI Dive
          e.preventDefault();
          this.state.openAiDive();
          break;

        case "c":
        case "C":
          // Toggle Constellation mode
          e.preventDefault();
          this.state.toggleConnectMode();
          break;

        case "m":
        case "M":
          // Toggle Reduced Motion
          e.preventDefault();
          this.state.toggleReducedMotion();
          break;

        case "h":
        case "H":
        case "?":
          // Open Shortcuts Help
          e.preventDefault();
          this.openHelpModal();
          break;

        case "ArrowRight":
        case "ArrowDown":
          // Cycle to next stock
          e.preventDefault();
          this.cycleSelectedStock(1);
          break;

        case "ArrowLeft":
        case "ArrowUp":
          // Cycle to previous stock
          e.preventDefault();
          this.cycleSelectedStock(-1);
          break;

        default:
          break;
      }
    }

    getOpenModal() {
      return [
        this.els.searchModal,
        this.els.aiDiveOverlay,
        this.els.helpModal,
      ].find(
        (modal) =>
          modal &&
          !modal.classList.contains("hidden") &&
          modal.getAttribute("aria-hidden") !== "true",
      );
    }

    trapModalFocus(event, modal) {
      const focusable = Array.from(
        modal.querySelectorAll(
          'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])',
        ),
      ).filter((element) => !element.hasAttribute("inert"));
      if (!focusable.length) {
        event.preventDefault();
        modal.focus();
        return;
      }
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    }

    captureModalReturnFocus(modal) {
      if (modal && !modal.contains(document.activeElement)) {
        this._modalReturnFocusTarget = document.activeElement;
      }
    }

    restoreModalFocus() {
      const returnFocusTarget = this._modalReturnFocusTarget;
      this._modalReturnFocusTarget = null;
      if (returnFocusTarget && document.contains(returnFocusTarget)) {
        returnFocusTarget.focus();
      }
    }

    cycleSelectedStock(direction) {
      const stocks = this.state.state.stockList;
      if (!stocks || !stocks.length) return;

      const current = this.state.state.selectedSymbol;
      let idx = stocks.findIndex((s) => s.symbol === current);
      if (idx === -1) idx = 0;

      const nextIdx = (idx + direction + stocks.length) % stocks.length;
      this.state.setSelectedSymbol(stocks[nextIdx].symbol);
    }

    announce(message) {
      if (!this.els.liveRegion || !message) return;
      this.els.liveRegion.textContent = message;
    }

    updatePauseUI(isPaused) {
      if (!this.els.pauseBtn) return;
      this.els.pauseBtn.classList.toggle("active", isPaused);
      this.els.pauseBtn.setAttribute("aria-pressed", String(isPaused));
      this.els.pauseBtn.title = isPaused
        ? "アニメーションを再開 (Space)"
        : "アニメーションを一時停止 (Space)";
      const icon = this.els.pauseBtn.querySelector(".btn-icon");
      if (icon) icon.textContent = isPaused ? "▶️" : "⏸";
      this.announce(
        isPaused
          ? "アニメーションが一時停止されました"
          : "アニメーションが再開されました",
      );
    }

    updateMotionUI(isReduced) {
      if (!this.els.motionBtn) return;
      this.els.motionBtn.classList.toggle("active", isReduced);
      this.els.motionBtn.setAttribute("aria-pressed", String(isReduced));
      this.els.motionBtn.title = isReduced
        ? "モーション効果を標準に戻す (M)"
        : "モーションを軽減する (M)";
      const icon = this.els.motionBtn.querySelector(".btn-icon");
      if (icon) icon.textContent = isReduced ? "⚡" : "✨";
      this.announce(
        isReduced
          ? "モーション軽減が有効になりました"
          : "標準アニメーションが有効になりました",
      );
    }

    updateScreenReaderTable(data) {
      const tableContainer = this.els.srTableContainer;
      if (!tableContainer) return;

      tableContainer.textContent = "";

      const table = document.createElement("table");
      table.setAttribute("role", "table");
      table.setAttribute("aria-label", "市場観測所 軌道銘柄一覧");

      const caption = document.createElement("caption");
      caption.textContent = `現在選択中の基準銘柄: ${data.selectedSymbol || "未選択"}`;
      table.appendChild(caption);

      const thead = document.createElement("thead");
      const hRow = document.createElement("tr");
      ["銘柄コード", "名称", "株価", "騰落率", "時価総額", "軌道位置"].forEach(
        (text) => {
          const th = document.createElement("th");
          th.setAttribute("scope", "col");
          th.textContent = text;
          hRow.appendChild(th);
        },
      );
      thead.appendChild(hRow);
      table.appendChild(thead);

      const tbody = document.createElement("tbody");
      const stocks = data.stockList || [];

      for (const st of stocks) {
        const tr = document.createElement("tr");
        if (st.symbol === data.selectedSymbol) {
          tr.className = "sr-selected-row";
        }

        const tdSym = document.createElement("td");
        tdSym.textContent = st.symbol;

        const tdName = document.createElement("td");
        tdName.textContent = st.displayName || st.name || st.symbol;

        const tdPrice = document.createElement("td");
        tdPrice.textContent =
          st.price > 0
            ? global.ObservatoryDataAdapter.formatPrice(st.price, st)
            : "--";

        const tdChg = document.createElement("td");
        const sign = st.changePercent >= 0 ? "+" : "";
        tdChg.textContent = `${sign}${st.changePercent.toFixed(2)}%`;

        const tdCap = document.createElement("td");
        tdCap.textContent =
          st.marketCap > 0
            ? global.ObservatoryDataAdapter.formatMarketCap(st.marketCap, st)
            : "--";

        const tdPos = document.createElement("td");
        tdPos.textContent =
          st.symbol === data.selectedSymbol
            ? "中央（基準銘柄）"
            : st.tier || "軌道上";

        tr.appendChild(tdSym);
        tr.appendChild(tdName);
        tr.appendChild(tdPrice);
        tr.appendChild(tdChg);
        tr.appendChild(tdCap);
        tr.appendChild(tdPos);
        tbody.appendChild(tr);
      }

      table.appendChild(tbody);
      tableContainer.appendChild(table);
    }

    openHelpModal() {
      if (!this.els.helpModal) return;
      this.captureModalReturnFocus(this.els.helpModal);
      this.els.helpModal.classList.remove("hidden");
      this.els.helpModal.setAttribute("aria-hidden", "false");
      this.els.helpModal.removeAttribute("inert");
      const closeBtn = this.els.helpModal.querySelector(
        "#shortcuts-help-close",
      );
      if (closeBtn) closeBtn.focus();
    }

    closeHelpModal() {
      if (!this.els.helpModal) return;
      this.els.helpModal.classList.add("hidden");
      this.els.helpModal.setAttribute("aria-hidden", "true");
      this.els.helpModal.setAttribute("inert", "");
      this.restoreModalFocus();
    }

    openSearchModal() {
      if (!this.els.searchModal) return;
      this.captureModalReturnFocus(this.els.searchModal);
      this.els.searchModal.classList.remove("hidden");
      this.els.searchModal.setAttribute("aria-hidden", "false");
      this.els.searchModal.removeAttribute("inert");
      if (this.els.searchInput) {
        this.els.searchInput.value = "";
        this.els.searchInput.focus();
        if (typeof this.els.onSearchInput === "function") {
          this.els.onSearchInput("");
        }
      }
    }

    closeSearchModal() {
      if (!this.els.searchModal) return;
      this.els.searchModal.classList.add("hidden");
      this.els.searchModal.setAttribute("aria-hidden", "true");
      this.els.searchModal.setAttribute("inert", "");
      this.restoreModalFocus();
    }
  }

  global.AccessibilityController = AccessibilityController;
})(typeof window !== "undefined" ? window : this);
