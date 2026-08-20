/**
 * orbit-renderer.js - High-performance Canvas 2D/2.5D rendering engine for Market Observatory.
 *
 * Renders starfields, tiered gravitational orbits, luminous stock nodes,
 * velocity trails, constellation lines, and drag-and-throw visual indicators.
 */

(function (global) {
  "use strict";

  class OrbitRenderer {
    constructor(canvas, state) {
      this.canvas = canvas;
      this.ctx = canvas.getContext("2d");
      this.state = state;

      this.width = 0;
      this.height = 0;
      this.dpr = 1;
      this.centerX = 0;
      this.centerY = 0;

      // Starfield particles
      this.stars = [];
      this.numStars = 90;

      // Node visual positions with smooth spring physics
      this.nodeRenderData = new Map();

      // Animation loop control
      this.animId = null;
      this.lastTime = performance.now();
      this.globalAngle = 0;

      // Orbit radii configurations
      this.radii = {
        inner: 140,
        middle: 240,
        outer: 350,
      };

      // 2.5D Orbit Tilt Factor
      this.tiltFactor = 0.72;

      // Supernova Shockwave Particle Effects
      this.shockwaves = [];

      this.initStars();
      this.resize();
      this.bindEvents();
    }

    triggerShockwave(x = this.centerX, y = this.centerY) {
      this.shockwaves.push({
        x,
        y,
        radius: 10,
        maxRadius: Math.max(this.width, this.height) * 0.45,
        alpha: 1.0,
        speed: 320, // px per sec
      });
    }

    initStars() {
      this.stars = [];
      for (let i = 0; i < this.numStars; i++) {
        this.stars.push({
          x: Math.random(),
          y: Math.random(),
          radius: Math.random() * 1.5 + 0.5,
          alpha: Math.random() * 0.7 + 0.2,
          speed: Math.random() * 0.0002 + 0.00005,
          twinkleSpeed: Math.random() * 0.02 + 0.005,
          twinkleOffset: Math.random() * Math.PI * 2,
        });
      }
    }

    resize() {
      if (!this.canvas) return;
      const rect = this.canvas.getBoundingClientRect();
      this.width = rect.width || window.innerWidth;
      this.height = rect.height || window.innerHeight;
      this.dpr = Math.min(window.devicePixelRatio || 1, 2);

      this.canvas.width = Math.floor(this.width * this.dpr);
      this.canvas.height = Math.floor(this.height * this.dpr);

      this.centerX = this.width / 2;
      this.centerY = this.height / 2;

      // Adjust radii dynamically based on viewport size
      const minDim = Math.min(this.width, this.height);
      const scaleFactor = Math.max(0.6, Math.min(minDim / 900, 1.2));
      this.radii = {
        inner: 130 * scaleFactor,
        middle: 230 * scaleFactor,
        outer: 340 * scaleFactor,
      };
    }

    bindEvents() {
      this._resizeHandler = () => this.resize();
      window.addEventListener("resize", this._resizeHandler);

      this._visibilityHandler = () => {
        if (document.hidden) {
          this.stop();
        } else {
          this.start();
        }
      };
      document.addEventListener("visibilitychange", this._visibilityHandler);
    }

    destroy() {
      this.stop();
      window.removeEventListener("resize", this._resizeHandler);
      document.removeEventListener("visibilitychange", this._visibilityHandler);
      this.nodeRenderData.clear();
    }

    start() {
      if (this.animId) return;
      this.lastTime = performance.now();
      const loop = (now) => {
        this.render(now);
        this.animId = requestAnimationFrame(loop);
      };
      this.animId = requestAnimationFrame(loop);
    }

    stop() {
      if (this.animId) {
        cancelAnimationFrame(this.animId);
        this.animId = null;
      }
    }

    getColorForChange(changePercent) {
      const isJpTheme = document.body.classList.contains("theme-color-jp");
      if (Math.abs(changePercent) < 0.05) {
        return {
          main: "#38bdf8", // Sky blue
          glow: "rgba(56, 189, 248, 0.45)",
          dark: "rgba(14, 45, 75, 0.85)",
        };
      }
      const isPositive = changePercent > 0;
      const isGreen = isJpTheme ? !isPositive : isPositive;

      if (isGreen) {
        return {
          main: "#10b981", // Emerald
          glow: "rgba(16, 185, 129, 0.55)",
          dark: "rgba(6, 44, 30, 0.85)",
        };
      } else {
        return {
          main: "#f43f5e", // Rose red
          glow: "rgba(244, 63, 94, 0.55)",
          dark: "rgba(54, 10, 20, 0.85)",
        };
      }
    }

    assignOrbits(stockList, selectedSymbol) {
      if (!stockList || !stockList.length) return [];
      const result = [];
      const nonCenterStocks = [];

      let centerStock = null;
      for (const s of stockList) {
        if (s.symbol === selectedSymbol) {
          centerStock = { ...s, isCenter: true, tier: "center" };
        } else {
          nonCenterStocks.push(s);
        }
      }

      if (centerStock) {
        result.push(centerStock);
      } else if (nonCenterStocks.length > 0) {
        centerStock = { ...nonCenterStocks[0], isCenter: true, tier: "center" };
        result.push(centerStock);
        nonCenterStocks.shift();
      }

      // Distribute remaining stocks into tiers
      // Tier 1 (Inner, 4-6 stocks): Portfolio or top favorites
      // Tier 2 (Middle, 6-10 stocks): Watchlist / High Volume
      // Tier 3 (Outer, remaining stocks): Market / Gainers / Losers
      const innerCount = Math.min(6, Math.ceil(nonCenterStocks.length * 0.3));
      const middleCount = Math.min(10, Math.ceil(nonCenterStocks.length * 0.4));

      nonCenterStocks.forEach((s, idx) => {
        let tier = "outer";
        let targetRadius = this.radii.outer;
        let tierIndex = idx - innerCount - middleCount;
        let tierTotal = Math.max(
          1,
          nonCenterStocks.length - innerCount - middleCount,
        );

        if (idx < innerCount) {
          tier = "inner";
          targetRadius = this.radii.inner;
          tierIndex = idx;
          tierTotal = innerCount;
        } else if (idx < innerCount + middleCount) {
          tier = "middle";
          targetRadius = this.radii.middle;
          tierIndex = idx - innerCount;
          tierTotal = middleCount;
        }

        const baseAngle = (tierIndex / tierTotal) * Math.PI * 2;
        result.push({
          ...s,
          isCenter: false,
          tier,
          targetRadius,
          baseAngle,
          tierIndex,
          tierTotal,
        });
      });

      return result;
    }

    render(now) {
      const dt = Math.min((now - this.lastTime) / 1000, 0.1);
      this.lastTime = now;

      const stateData = this.state.state;
      const isPaused = stateData.paused;
      const isReducedMotion = stateData.reducedMotion;

      if (!isPaused && !isReducedMotion) {
        this.globalAngle += dt * 0.15;
      }

      const ctx = this.ctx;
      ctx.save();
      ctx.scale(this.dpr, this.dpr);

      // 1. Cosmic Deep Space Background
      this.renderBackground(ctx, now);

      // 2. Starfield Particles
      this.renderStarfield(ctx, now);

      // 3. Concentric Orbital Tracks & Gravitational Grid
      this.renderOrbitTracks(ctx, now);

      // 4. Center Drop Zone Highlight (if dragging)
      this.renderCenterDropZone(ctx, stateData, now);

      // 4.5. Supernova Shockwaves
      this.renderShockwaves(ctx, dt);

      // 5. Orbital & Center Nodes
      const assigned = this.assignOrbits(
        stateData.stockList,
        stateData.selectedSymbol,
      );
      this.updateAndRenderNodes(ctx, assigned, stateData, dt, now);

      // 6. Constellation Connecting Lines
      this.renderConstellations(ctx, stateData, now);

      // 7. Dragged Node Overlay (if any)
      this.renderDraggedNode(ctx, stateData, now);

      // 8. Hover HUD Tooltip (if hovering)
      this.renderHoverTooltip(ctx, stateData);

      ctx.restore();
    }

    renderShockwaves(ctx, dt) {
      for (let i = this.shockwaves.length - 1; i >= 0; i--) {
        const sw = this.shockwaves[i];
        sw.radius += sw.speed * dt;
        sw.alpha = Math.max(0, 1.0 - sw.radius / sw.maxRadius);
        if (sw.alpha <= 0 || sw.radius >= sw.maxRadius) {
          this.shockwaves.splice(i, 1);
          continue;
        }
        ctx.save();
        ctx.strokeStyle = `rgba(16, 185, 129, ${sw.alpha.toFixed(2)})`;
        ctx.lineWidth = Math.max(1, 3.5 * sw.alpha);
        ctx.beginPath();
        ctx.arc(sw.x, sw.y, sw.radius, 0, Math.PI * 2);
        ctx.stroke();
        ctx.restore();
      }
    }

    renderBackground(ctx, now) {
      // Deep cosmic gradient
      const bgGrad = ctx.createRadialGradient(
        this.centerX,
        this.centerY,
        20,
        this.centerX,
        this.centerY,
        Math.max(this.width, this.height) * 0.85,
      );
      bgGrad.addColorStop(0, "#0e1430");
      bgGrad.addColorStop(0.35, "#080c1d");
      bgGrad.addColorStop(0.7, "#04060f");
      bgGrad.addColorStop(1, "#020308");

      ctx.fillStyle = bgGrad;
      ctx.fillRect(0, 0, this.width, this.height);

      // Celestial nebula cloud glow
      const timeSec = now * 0.0003;
      const nebulaX = this.centerX + Math.sin(timeSec) * 40;
      const nebulaY = this.centerY + Math.cos(timeSec) * 30;

      const nebulaGrad = ctx.createRadialGradient(
        nebulaX,
        nebulaY,
        10,
        nebulaX,
        nebulaY,
        this.radii.outer * 1.3,
      );
      nebulaGrad.addColorStop(0, "rgba(99, 102, 241, 0.08)");
      nebulaGrad.addColorStop(0.5, "rgba(168, 85, 247, 0.04)");
      nebulaGrad.addColorStop(1, "rgba(0, 0, 0, 0)");

      ctx.fillStyle = nebulaGrad;
      ctx.fillRect(0, 0, this.width, this.height);
    }

    renderStarfield(ctx, now) {
      for (const star of this.stars) {
        const x = star.x * this.width;
        const y = star.y * this.height;
        const twinkle =
          Math.sin(now * star.twinkleSpeed + star.twinkleOffset) * 0.3 + 0.7;
        const alpha = Math.max(0.1, Math.min(star.alpha * twinkle, 1));

        ctx.fillStyle = `rgba(220, 235, 255, ${alpha.toFixed(3)})`;
        ctx.beginPath();
        ctx.arc(x, y, star.radius, 0, Math.PI * 2);
        ctx.fill();
      }
    }

    renderOrbitTracks(ctx, now) {
      const tiers = [
        {
          radius: this.radii.inner,
          color: "rgba(99, 102, 241, 0.28)",
          dash: [4, 6],
          label: "PORTFOLIO",
        },
        {
          radius: this.radii.middle,
          color: "rgba(56, 189, 248, 0.22)",
          dash: [6, 8],
          label: "WATCHLIST",
        },
        {
          radius: this.radii.outer,
          color: "rgba(168, 85, 247, 0.16)",
          dash: [8, 12],
          label: "MARKET",
        },
      ];

      const timeOffset = (now * 0.008) % 360;

      for (const tier of tiers) {
        ctx.save();
        ctx.strokeStyle = tier.color;
        ctx.lineWidth = 1.2;
        ctx.setLineDash(tier.dash);
        ctx.lineDashOffset = -timeOffset;

        ctx.beginPath();
        ctx.ellipse(
          this.centerX,
          this.centerY,
          tier.radius,
          tier.radius * this.tiltFactor,
          0,
          0,
          Math.PI * 2,
        );
        ctx.stroke();

        // Subtle tier label on orbit ring
        ctx.fillStyle = tier.color.replace("0.", "0.4");
        ctx.font = "9px 'Inter', sans-serif";
        ctx.textAlign = "left";
        ctx.textBaseline = "middle";
        ctx.fillText(tier.label, this.centerX + tier.radius + 6, this.centerY);
        ctx.restore();
      }
    }

    renderCenterDropZone(ctx, stateData, now) {
      if (!stateData.draggedSymbol) return;

      const isHovered = stateData.isOverCenterDrop;
      const pulse = Math.sin(now * 0.008) * 6;
      const dropRadius = 65 + pulse;

      ctx.save();
      const dropGrad = ctx.createRadialGradient(
        this.centerX,
        this.centerY,
        10,
        this.centerX,
        this.centerY,
        dropRadius + 20,
      );

      if (isHovered) {
        dropGrad.addColorStop(0, "rgba(16, 185, 129, 0.45)");
        dropGrad.addColorStop(0.7, "rgba(16, 185, 129, 0.15)");
        dropGrad.addColorStop(1, "rgba(16, 185, 129, 0)");
        ctx.strokeStyle = "#10b981";
        ctx.lineWidth = 2.5;
        ctx.setLineDash([6, 4]);
      } else {
        dropGrad.addColorStop(0, "rgba(99, 102, 241, 0.35)");
        dropGrad.addColorStop(0.7, "rgba(99, 102, 241, 0.10)");
        dropGrad.addColorStop(1, "rgba(99, 102, 241, 0)");
        ctx.strokeStyle = "rgba(99, 102, 241, 0.7)";
        ctx.lineWidth = 1.5;
        ctx.setLineDash([4, 6]);
      }

      ctx.fillStyle = dropGrad;
      ctx.beginPath();
      ctx.arc(this.centerX, this.centerY, dropRadius + 20, 0, Math.PI * 2);
      ctx.fill();

      ctx.beginPath();
      ctx.arc(this.centerX, this.centerY, dropRadius, 0, Math.PI * 2);
      ctx.stroke();

      // Text indicator
      ctx.fillStyle = isHovered ? "#6ee7b7" : "#c7d2fe";
      ctx.font = "bold 11px 'Inter', sans-serif";
      ctx.textAlign = "center";
      ctx.textBaseline = "middle";
      ctx.fillText(
        isHovered ? "RELEASE TO FOCUS" : "DROP TO FOCUS",
        this.centerX,
        this.centerY + dropRadius + 22,
      );

      ctx.restore();
    }

    updateAndRenderNodes(ctx, assignedStocks, stateData, dt, now) {
      // 1. Orbital Anti-collision spacing pass
      const tierMap = new Map();
      for (const stock of assignedStocks) {
        if (!stock || stock.isCenter) continue;
        const tierKey = stock.tier || "middle";
        if (!tierMap.has(tierKey)) tierMap.set(tierKey, []);
        tierMap.get(tierKey).push(stock);
      }

      for (const [tierKey, list] of tierMap.entries()) {
        if (list.length < 2) continue;
        const radius =
          tierKey === "inner"
            ? this.radii.inner
            : tierKey === "middle"
              ? this.radii.middle
              : this.radii.outer;
        const minAngleSpacing = 36 / radius; // Angular space threshold

        for (let i = 0; i < list.length; i++) {
          for (let j = i + 1; j < list.length; j++) {
            const rDataA = this.nodeRenderData.get(list[i].symbol);
            const rDataB = this.nodeRenderData.get(list[j].symbol);
            if (!rDataA || !rDataB) continue;

            let diff = rDataB.angle - rDataA.angle;
            // Normalize angle diff to [-PI, PI]
            while (diff < -Math.PI) diff += Math.PI * 2;
            while (diff > Math.PI) diff -= Math.PI * 2;

            if (Math.abs(diff) < minAngleSpacing) {
              const push = (minAngleSpacing - Math.abs(diff)) * 0.5;
              const dir = diff >= 0 ? 1 : -1;
              rDataA.angle -= push * dir * 0.2;
              rDataB.angle += push * dir * 0.2;
            }
          }
        }
      }

      // 2. Position calculation & rendering pass
      for (const stock of assignedStocks) {
        if (!stock || !stock.symbol) continue;

        let renderData = this.nodeRenderData.get(stock.symbol);
        if (!renderData) {
          renderData = {
            currentX: this.centerX,
            currentY: this.centerY,
            currentRadius: stock.radius || 20,
            angle: stock.baseAngle || 0,
            trail: [],
          };
          this.nodeRenderData.set(stock.symbol, renderData);
        }

        // Calculate target position
        let targetX = this.centerX;
        let targetY = this.centerY;
        let baseRadius = stock.radius || 22;
        let depthScale = 1.0;

        if (stock.isCenter) {
          baseRadius = 34; // Center focal size
        } else {
          // Angular orbit movement
          const orbitSpeed =
            (stock.volatility || 1.0) *
            0.18 *
            (stock.tier === "inner"
              ? 1.2
              : stock.tier === "middle"
                ? 0.8
                : 0.5);
          if (!stateData.paused && !stateData.reducedMotion) {
            renderData.angle += dt * orbitSpeed;
          }

          const r = stock.targetRadius || this.radii.middle;
          targetX = this.centerX + Math.cos(renderData.angle) * r;
          targetY =
            this.centerY + Math.sin(renderData.angle) * r * this.tiltFactor;

          // Z-Depth scaling (front is larger, back is slightly smaller)
          const zDepth = Math.sin(renderData.angle); // -1 (top/back) to 1 (bottom/front)
          depthScale = 0.88 + (zDepth + 1) * 0.14; // 0.88 to 1.16
        }

        const targetRadius = baseRadius * depthScale;

        // Spring interpolation towards target
        const lerpFactor = stateData.reducedMotion
          ? 1.0
          : Math.min(1.0, dt * 10);
        renderData.currentX += (targetX - renderData.currentX) * lerpFactor;
        renderData.currentY += (targetY - renderData.currentY) * lerpFactor;
        renderData.currentRadius +=
          (targetRadius - renderData.currentRadius) * lerpFactor;

        // Skip rendering on main pass if currently dragged (handled in renderDraggedNode)
        if (stateData.draggedSymbol === stock.symbol) {
          continue;
        }

        this.renderSingleNode(
          ctx,
          stock,
          renderData.currentX,
          renderData.currentY,
          renderData.currentRadius,
          stateData,
          now,
        );
      }
    }

    renderSingleNode(ctx, stock, x, y, r, stateData, now) {
      const isHovered = stateData.hoveredSymbol === stock.symbol;
      const isSelected = stateData.selectedSymbol === stock.symbol;
      const isConnected = stateData.connectedSymbols.includes(stock.symbol);
      const isCenter = stock.isCenter;

      const colors = this.getColorForChange(stock.changePercent);
      const displayRadius = isHovered ? r * 1.18 : r;

      ctx.save();

      // 1. Center Core Aura Waves
      if (isCenter) {
        const pulse = Math.sin(now * 0.005) * 8;
        const auraGrad = ctx.createRadialGradient(
          x,
          y,
          displayRadius * 0.8,
          x,
          y,
          displayRadius * 2.2 + pulse,
        );
        auraGrad.addColorStop(0, colors.glow);
        auraGrad.addColorStop(0.6, "rgba(99, 102, 241, 0.2)");
        auraGrad.addColorStop(1, "rgba(0, 0, 0, 0)");

        ctx.fillStyle = auraGrad;
        ctx.beginPath();
        ctx.arc(x, y, displayRadius * 2.2 + pulse, 0, Math.PI * 2);
        ctx.fill();
      }

      // 2. Node Outer Glow & Halo
      if (isHovered || isSelected || isConnected) {
        ctx.shadowColor = colors.main;
        ctx.shadowBlur = isHovered ? 20 : 12;
      } else {
        ctx.shadowColor = colors.glow;
        ctx.shadowBlur = 6;
      }

      // 3. Node Circle Background Fill
      const nodeGrad = ctx.createRadialGradient(
        x - displayRadius * 0.3,
        y - displayRadius * 0.3,
        2,
        x,
        y,
        displayRadius,
      );
      nodeGrad.addColorStop(0, "#1f293d");
      nodeGrad.addColorStop(0.7, "#0d1322");
      nodeGrad.addColorStop(1, "#080c16");

      ctx.fillStyle = nodeGrad;
      ctx.beginPath();
      ctx.arc(x, y, displayRadius, 0, Math.PI * 2);
      ctx.fill();

      // 4. Node Border
      ctx.strokeStyle = isConnected
        ? "#f59e0b"
        : isHovered
          ? "#ffffff"
          : colors.main;
      ctx.lineWidth = isConnected
        ? 3.0
        : isHovered
          ? 2.5
          : isCenter
            ? 3.0
            : 1.8;
      ctx.stroke();

      // Reset shadow for text rendering
      ctx.shadowBlur = 0;

      // 5. Node Text & Financial Badge
      ctx.textAlign = "center";
      ctx.textBaseline = "middle";

      // Symbol Ticker
      ctx.fillStyle = "#ffffff";
      const fontSize = isCenter
        ? 13
        : Math.max(10, Math.min(displayRadius * 0.6, 12));
      ctx.font = `bold ${fontSize}px 'Orbitron', 'Inter', sans-serif`;
      ctx.fillText(stock.symbol, x, isCenter ? y - 7 : y - 4);

      // Price / Change Pill
      const chgSign = stock.changePercent >= 0 ? "+" : "";
      const chgStr = `${chgSign}${stock.changePercent.toFixed(1)}%`;
      ctx.fillStyle = colors.main;
      ctx.font = `600 ${Math.max(8, fontSize - 2)}px 'Inter', sans-serif`;
      ctx.fillText(chgStr, x, isCenter ? y + 8 : y + 7);

      // Focus / Connection Ring Indicator
      if (isConnected) {
        const connIdx = stateData.connectedSymbols.indexOf(stock.symbol) + 1;
        ctx.fillStyle = "#f59e0b";
        ctx.beginPath();
        ctx.arc(
          x + displayRadius * 0.7,
          y - displayRadius * 0.7,
          8,
          0,
          Math.PI * 2,
        );
        ctx.fill();
        ctx.fillStyle = "#000000";
        ctx.font = "bold 9px 'Inter', sans-serif";
        ctx.fillText(
          String(connIdx),
          x + displayRadius * 0.7,
          y - displayRadius * 0.7,
        );
      }

      ctx.restore();
    }

    renderConstellations(ctx, stateData, now) {
      const symbols = stateData.connectedSymbols;
      if (!symbols || symbols.length < 2) return;

      const points = [];
      for (const sym of symbols) {
        const renderData = this.nodeRenderData.get(sym);
        if (renderData) {
          points.push({
            symbol: sym,
            x: renderData.currentX,
            y: renderData.currentY,
          });
        }
      }

      if (points.length < 2) return;

      ctx.save();
      const pulseAlpha = Math.sin(now * 0.006) * 0.25 + 0.65;
      ctx.strokeStyle = `rgba(245, 158, 11, ${pulseAlpha.toFixed(3)})`;
      ctx.lineWidth = 2.0;
      ctx.setLineDash([6, 6]);

      for (let i = 0; i < points.length; i++) {
        const next = points[(i + 1) % points.length];
        if (points.length === 2 && i === 1) break; // Don't double draw for 2 points

        ctx.beginPath();
        ctx.moveTo(points[i].x, points[i].y);
        ctx.lineTo(next.x, next.y);
        ctx.stroke();

        // Midpoint connector energy dot
        const midX = (points[i].x + next.x) / 2;
        const midY = (points[i].y + next.y) / 2;
        ctx.fillStyle = "#f59e0b";
        ctx.beginPath();
        ctx.arc(midX, midY, 3.5, 0, Math.PI * 2);
        ctx.fill();
      }

      ctx.restore();
    }

    renderDraggedNode(ctx, stateData, now) {
      if (!stateData.draggedSymbol || !stateData.dragPos) return;

      const stock = stateData.stocks.get(stateData.draggedSymbol);
      if (!stock) return;

      const { x, y } = stateData.dragPos;
      const r = (stock.radius || 20) * 1.35;

      ctx.save();

      // Energy tether beam connecting dragged node to center
      ctx.strokeStyle = stateData.isOverCenterDrop
        ? "rgba(16, 185, 129, 0.7)"
        : "rgba(99, 102, 241, 0.4)";
      ctx.lineWidth = 2.0;
      ctx.setLineDash([4, 4]);
      ctx.beginPath();
      ctx.moveTo(this.centerX, this.centerY);
      ctx.lineTo(x, y);
      ctx.stroke();

      // Render the floating dragged node
      this.renderSingleNode(ctx, stock, x, y, r, stateData, now);

      ctx.restore();
    }

    renderHoverTooltip(ctx, stateData) {
      const symbol = stateData.hoveredSymbol;
      if (!symbol || stateData.draggedSymbol) return;

      const stock = stateData.stocks.get(symbol);
      const renderData = this.nodeRenderData.get(symbol);
      if (!stock || !renderData) return;

      const { currentX: x, currentY: y, currentRadius: r } = renderData;
      const cardW = 180;
      const cardH = 92;

      // Smart position offset to keep within bounds
      let posX = x + r + 12;
      let posY = y - cardH / 2;

      if (posX + cardW > this.width - 12) {
        posX = x - r - cardW - 12;
      }
      if (posY < 12) posY = 12;
      if (posY + cardH > this.height - 12) posY = this.height - cardH - 12;

      ctx.save();
      // Backdrop blur fill
      ctx.fillStyle = "rgba(15, 23, 42, 0.90)";
      ctx.strokeStyle = "rgba(99, 102, 241, 0.6)";
      ctx.lineWidth = 1.5;

      ctx.beginPath();
      ctx.roundRect
        ? ctx.roundRect(posX, posY, cardW, cardH, 8)
        : ctx.rect(posX, posY, cardW, cardH);
      ctx.fill();
      ctx.stroke();

      // Content text
      const colors = this.getColorForChange(stock.changePercent);

      // Symbol Title
      ctx.fillStyle = "#ffffff";
      ctx.font = "bold 12px 'Orbitron', sans-serif";
      ctx.textAlign = "left";
      ctx.textBaseline = "top";
      ctx.fillText(stock.symbol, posX + 10, posY + 10);

      // Sector Tag
      ctx.fillStyle = "rgba(148, 163, 184, 0.85)";
      ctx.font = "10px 'Inter', sans-serif";
      ctx.fillText(stock.sector || "その他", posX + 75, posY + 12);

      // Name Subtitle
      ctx.fillStyle = "#94a3b8";
      ctx.font = "10px 'Inter', sans-serif";
      const truncatedName =
        (stock.displayName || stock.name || "").length > 18
          ? (stock.displayName || stock.name).substring(0, 18) + "..."
          : stock.displayName || stock.name;
      ctx.fillText(truncatedName, posX + 10, posY + 28);

      // Price & Change
      const priceStr =
        stock.price > 0
          ? global.ObservatoryDataAdapter.formatPrice(stock.price, stock)
          : "--";
      ctx.fillStyle = "#ffffff";
      ctx.font = "bold 13px 'Inter', sans-serif";
      ctx.fillText(priceStr, posX + 10, posY + 46);

      const chgSign = stock.changePercent >= 0 ? "+" : "";
      ctx.fillStyle = colors.main;
      ctx.font = "bold 11px 'Inter', sans-serif";
      ctx.fillText(
        `${chgSign}${stock.changePercent.toFixed(2)}%`,
        posX + 90,
        posY + 48,
      );

      // Hint line
      ctx.fillStyle = "rgba(148, 163, 184, 0.7)";
      ctx.font = "9px 'Inter', sans-serif";
      ctx.fillText(
        "長押し / D: AI Dive | ドロップ: 基準変更",
        posX + 10,
        posY + 72,
      );

      ctx.restore();
    }

    /**
     * Hit test: Find stock node at client coordinates (canvas relative)
     */
    hitTest(clientX, clientY) {
      const stateData = this.state.state;
      const stocks = stateData.stockList;
      if (!stocks || !stocks.length) return null;

      // Check in reverse order so topmost/rendered nodes receive hits first
      for (let i = stocks.length - 1; i >= 0; i--) {
        const s = stocks[i];
        const renderData = this.nodeRenderData.get(s.symbol);
        if (!renderData) continue;

        const dx = clientX - renderData.currentX;
        const dy = clientY - renderData.currentY;
        const hitRadius = (renderData.currentRadius || 20) + 6;

        if (dx * dx + dy * dy <= hitRadius * hitRadius) {
          return s;
        }
      }
      return null;
    }
  }

  global.OrbitRenderer = OrbitRenderer;
})(typeof window !== "undefined" ? window : this);
