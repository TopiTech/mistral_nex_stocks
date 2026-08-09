/**
 * gesture-controller.js - Pointer and gesture interactions for Market Observatory.
 *
 * Handles Pointer Events for dragging nodes to the center drop-zone,
 * 600ms long-press for AI Dive, node selection, and Constellation links.
 */

(function (global) {
  "use strict";

  class GestureController {
    constructor(canvas, state, renderer) {
      this.canvas = canvas;
      this.state = state;
      this.renderer = renderer;

      this.pointerDownTarget = null;
      this.pointerDownPos = { x: 0, y: 0 };
      this.isDragging = false;
      this.dragThreshold = 6;
      this.longPressTimer = null;
      this.longPressDuration = 600; // 600ms long-press

      this.bindEvents();
    }

    bindEvents() {
      this._onPointerDown = (e) => this.handlePointerDown(e);
      this._onPointerMove = (e) => this.handlePointerMove(e);
      this._onPointerUp = (e) => this.handlePointerUp(e);
      this._onPointerCancel = (e) => this.handlePointerCancel(e);

      this.canvas.addEventListener("pointerdown", this._onPointerDown);
      window.addEventListener("pointermove", this._onPointerMove);
      window.addEventListener("pointerup", this._onPointerUp);
      window.addEventListener("pointercancel", this._onPointerCancel);
    }

    destroy() {
      this.clearLongPressTimer();
      this.canvas.removeEventListener("pointerdown", this._onPointerDown);
      window.removeEventListener("pointermove", this._onPointerMove);
      window.removeEventListener("pointerup", this._onPointerUp);
      window.removeEventListener("pointercancel", this._onPointerCancel);
    }

    getCanvasPos(e) {
      const rect = this.canvas.getBoundingClientRect();
      return {
        x: e.clientX - rect.left,
        y: e.clientY - rect.top,
      };
    }

    handlePointerDown(e) {
      if (e.button !== 0 && e.pointerType === "mouse") return; // Left click only for mouse
      const pos = this.getCanvasPos(e);
      const hitNode = this.renderer.hitTest(pos.x, pos.y);

      this.pointerDownTarget = hitNode;
      this.pointerDownPos = pos;
      this.isDragging = false;

      this.clearLongPressTimer();

      if (hitNode) {
        // Start long-press detection
        this.longPressTimer = setTimeout(() => {
          this.longPressTimer = null;
          if (!this.isDragging && this.pointerDownTarget) {
            // Trigger AI Dive for long-pressed stock
            this.state.openAiDive(this.pointerDownTarget.symbol);
            if (typeof global.showToast === "function") {
              global.showToast(
                `✨ AI Dive: ${this.pointerDownTarget.symbol}`,
                "#6366f1",
              );
            }
          }
        }, this.longPressDuration);
      }
    }

    handlePointerMove(e) {
      const pos = this.getCanvasPos(e);

      // 1. If pointer is down and threshold exceeded, initiate dragging
      if (this.pointerDownTarget && !this.isDragging) {
        const dx = pos.x - this.pointerDownPos.x;
        const dy = pos.y - this.pointerDownPos.y;
        if (dx * dx + dy * dy >= this.dragThreshold * this.dragThreshold) {
          this.isDragging = true;
          this.clearLongPressTimer();
          this.canvas.style.cursor = "grabbing";
        }
      }

      // 2. If actively dragging
      if (this.isDragging && this.pointerDownTarget) {
        const centerX = this.renderer.centerX;
        const centerY = this.renderer.centerY;
        const distSq = (pos.x - centerX) ** 2 + (pos.y - centerY) ** 2;
        const dropRadius = 75;
        const isOverCenter = distSq <= dropRadius * dropRadius;

        this.state.setDraggedSymbol(
          this.pointerDownTarget.symbol,
          pos,
          isOverCenter,
        );
        return;
      }

      // 3. Hover state when not dragging
      const hitNode = this.renderer.hitTest(pos.x, pos.y);
      if (hitNode) {
        this.canvas.style.cursor = this.state.state.isConnectMode
          ? "crosshair"
          : "pointer";
        this.state.setHoveredSymbol(hitNode.symbol);
      } else {
        this.canvas.style.cursor = "default";
        this.state.setHoveredSymbol(null);
      }
    }

    handlePointerUp(_e) {
      this.clearLongPressTimer();

      if (this.isDragging && this.pointerDownTarget) {
        const stateData = this.state.state;
        if (stateData.isOverCenterDrop) {
          // Successfully thrown into center!
          const newSymbol = this.pointerDownTarget.symbol;
          this.state.setSelectedSymbol(newSymbol);
          if (
            this.renderer &&
            typeof this.renderer.triggerShockwave === "function"
          ) {
            this.renderer.triggerShockwave();
          }
          if (typeof global.showToast === "function") {
            global.showToast(
              `🎯 基準銘柄を ${newSymbol} に変更しました`,
              "#10b981",
            );
          }
        }
        // End drag
        this.state.setDraggedSymbol(null, null, false);
        this.isDragging = false;
        this.pointerDownTarget = null;
        this.canvas.style.cursor = "default";
        return;
      }

      // If it was a click/tap without dragging
      if (this.pointerDownTarget && !this.isDragging) {
        const hit = this.pointerDownTarget;
        if (this.state.state.isConnectMode) {
          // Constellation connect toggle
          this.state.toggleConnectedSymbol(hit.symbol);
        } else {
          // Select or inspect
          this.state.setSelectedSymbol(hit.symbol);
        }
      }

      this.pointerDownTarget = null;
      this.isDragging = false;
      this.canvas.style.cursor = "default";
    }

    handlePointerCancel(_e) {
      this.clearLongPressTimer();
      if (this.isDragging) {
        this.state.setDraggedSymbol(null, null, false);
      }
      this.pointerDownTarget = null;
      this.isDragging = false;
      this.canvas.style.cursor = "default";
    }

    clearLongPressTimer() {
      if (this.longPressTimer) {
        clearTimeout(this.longPressTimer);
        this.longPressTimer = null;
      }
    }
  }

  global.GestureController = GestureController;
})(typeof window !== "undefined" ? window : this);
