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
      // (L28, 2026-11-07 review) activePointerId 以外の up/cancel で gesture
      // 状態を壊さないためのガード。従来は第二本の pointerdown が
      // pointerDownTarget を無条件に上書きし、第一指の up が第二指の
      // ノードに作用する(誤選択/AI 誤起動)ほか、第二指の移動が判定距離に
      // 加算されるなど、単一ポインタ前提の状態がすべて破綻していた。
      this.activePointerId = null;
      this.isDragging = false;
      this.dragThreshold = 6;
      this.longPressTimer = null;
      this.longPressDuration = 600; // 600ms long-press
      this.longPressTriggered = false;

      this.bindEvents();
    }

    bindEvents() {
      this._onPointerDown = (e) => this.handlePointerDown(e);
      this._onPointerMove = (e) => this.handlePointerMove(e);
      this._onPointerUp = (e) => this.handlePointerUp(e);
      this._onPointerCancel = (e) => this.handlePointerCancel(e);
      this._onPointerLeave = () => this.handlePointerLeave();

      this.canvas.addEventListener("pointerdown", this._onPointerDown);
      this.canvas.addEventListener("pointerleave", this._onPointerLeave);
      window.addEventListener("pointermove", this._onPointerMove);
      window.addEventListener("pointerup", this._onPointerUp);
      window.addEventListener("pointercancel", this._onPointerCancel);
      window.addEventListener("blur", this._onPointerCancel);
    }

    destroy() {
      this.clearLongPressTimer();
      this.canvas.removeEventListener("pointerdown", this._onPointerDown);
      this.canvas.removeEventListener("pointerleave", this._onPointerLeave);
      window.removeEventListener("pointermove", this._onPointerMove);
      window.removeEventListener("pointerup", this._onPointerUp);
      window.removeEventListener("pointercancel", this._onPointerCancel);
      window.removeEventListener("blur", this._onPointerCancel);
    }

    handlePointerLeave() {
      if (!this.isDragging) {
        if (this.state.state.hoveredSymbol !== null) {
          this.state.setHoveredSymbol(null);
        }
        if (this.canvas && this.canvas.style) {
          this.canvas.style.cursor = "default";
        }
      }
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
      // (L28) すでにアクティブなポインタがある場合は新規 pointerdown を
      // 無視する(既存のドラッグ/ロングプレス/選択状態を維持する)。
      if (this.activePointerId !== null) {
        if (e.pointerId !== this.activePointerId) return;
      } else {
        this.activePointerId = e.pointerId;
      }
      const pos = this.getCanvasPos(e);
      const hitNode = this.renderer.hitTest(pos.x, pos.y);

      this.pointerDownTarget = hitNode;
      this.pointerDownPos = pos;
      this.isDragging = false;
      this.longPressTriggered = false;

      this.clearLongPressTimer();

      if (hitNode) {
        // Start long-press detection
        this.longPressTimer = setTimeout(() => {
          this.longPressTimer = null;
          if (!this.isDragging && this.pointerDownTarget) {
            this.longPressTriggered = true;
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
      // (L28) アクティブポインタ以外の移動は gesture 計算に使わない。
      if (this.activePointerId !== null && e.pointerId !== this.activePointerId)
        return;
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
      // モーダル/ドロワー等のオーバーレイ越しの pointermove ではホバー状態を
      // 書き換えない(背面ノードのツールチップが、パネルを閉じた瞬間に
      // 出現するのを防ぐ。ドラッグ中は上の分岐で追跡を継続する)。
      if (e.target !== this.canvas) {
        return;
      }
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

    handlePointerUp(e) {
      // (L28) アクティブポインタ以外の up は gesture 状態を releasing しない。
      if (this.activePointerId !== null && e.pointerId !== this.activePointerId)
        return;
      this.activePointerId = null;
      this.clearLongPressTimer();

      if (this.longPressTriggered) {
        this.longPressTriggered = false;
        this.pointerDownTarget = null;
        this.isDragging = false;
        this.canvas.style.cursor = "default";
        return;
      }

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

    handlePointerCancel(e) {
      // (L28) アクティブポインタ以外の cancel は無視する。ただし
      // window blur (ポインタではないイベント)は pointerId を持たない
      // ため、従来どおり全ポインタを abandon する。
      if (
        this.activePointerId !== null &&
        e.pointerId !== undefined &&
        e.pointerId !== this.activePointerId
      )
        return;
      this.activePointerId = null;
      this.clearLongPressTimer();
      this.longPressTriggered = false;
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
