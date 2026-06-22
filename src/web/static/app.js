/* ============================================================
   MNEME — Tactical Dashboard JavaScript
   Constellation · Glow Cards · GSAP Boot · Tabs · D3 Graph
   ============================================================ */

(function () {
  'use strict';

  const REDUCED_MOTION = window.matchMedia('(prefers-reduced-motion: reduce)').matches;

  /* ──────────────────────────────────────────────────────────
     1. CONSTELLATION GRAPH (Canvas)
     ────────────────────────────────────────────────────────── */
  function initConstellation() {
    const canvas = document.getElementById('constellation-canvas');
    if (!canvas) return;

    const ctx = canvas.getContext('2d');
    let w, h;
    const NODE_COUNT = 16;
    const CYCLE_DURATION = 14; // seconds
    const ANIM_DURATION = 2.5;
    let nodes = [];
    let connections = [];

    function resize() {
      w = canvas.width = window.innerWidth;
      h = canvas.height = window.innerHeight;
    }

    function createNodes() {
      nodes = [];
      connections = [];
      for (let i = 0; i < NODE_COUNT; i++) {
        nodes.push({
          x: Math.random() * w,
          y: Math.random() * h,
          originX: Math.random() * w,
          originY: Math.random() * h,
          targetX: w * 0.3 + Math.random() * w * 0.4,
          targetY: h * 0.3 + Math.random() * h * 0.4,
          radius: 1.5 + Math.random() * 1.5,
          progress: 0, // 0 = raw (cyan), 1 = synthesized (amber)
        });
      }

      // Build 1-3 connections per node to nearby nodes
      for (let i = 0; i < NODE_COUNT; i++) {
        const numLinks = 1 + Math.floor(Math.random() * 3);
        const distances = [];
        for (let j = 0; j < NODE_COUNT; j++) {
          if (i === j) continue;
          const dx = nodes[i].targetX - nodes[j].targetX;
          const dy = nodes[i].targetY - nodes[j].targetY;
          distances.push({ idx: j, dist: Math.sqrt(dx * dx + dy * dy) });
        }
        distances.sort((a, b) => a.dist - b.dist);
        for (let k = 0; k < Math.min(numLinks, distances.length); k++) {
          const pair = [Math.min(i, distances[k].idx), Math.max(i, distances[k].idx)].join('-');
          if (!connections.find(c => c.pair === pair)) {
            connections.push({
              pair,
              a: i,
              b: distances[k].idx,
              dashOffset: 0,
              progress: 0,
            });
          }
        }
      }
    }

    function lerpColor(t) {
      // cyan (#2BD9CB) → amber (#FFAB00)
      const r = Math.round(43 + (255 - 43) * t);
      const g = Math.round(217 + (171 - 217) * t);
      const b = Math.round(203 + (0 - 203) * t);
      return `rgb(${r},${g},${b})`;
    }

    function draw() {
      ctx.clearRect(0, 0, w, h);

      // Draw connections
      connections.forEach(conn => {
        if (conn.progress <= 0) return;
        const a = nodes[conn.a];
        const b = nodes[conn.b];
        ctx.beginPath();
        ctx.moveTo(a.x, a.y);
        ctx.lineTo(b.x, b.y);
        ctx.strokeStyle = lerpColor(conn.progress);
        ctx.globalAlpha = 0.3 * conn.progress;
        ctx.lineWidth = 0.8;
        ctx.setLineDash([4, 6]);
        ctx.lineDashOffset = conn.dashOffset;
        ctx.stroke();
        ctx.setLineDash([]);
        ctx.globalAlpha = 1;
      });

      // Draw nodes
      nodes.forEach(node => {
        ctx.beginPath();
        ctx.arc(node.x, node.y, node.radius, 0, Math.PI * 2);
        ctx.fillStyle = lerpColor(node.progress);
        ctx.globalAlpha = 0.6 + 0.4 * node.progress;
        ctx.fill();
        ctx.globalAlpha = 1;
      });
    }

    function animateCycle() {
      if (REDUCED_MOTION) {
        // Show end state immediately
        nodes.forEach(n => {
          n.x = n.targetX;
          n.y = n.targetY;
          n.progress = 1;
        });
        connections.forEach(c => { c.progress = 1; });
        draw();
        return;
      }

      const tl = gsap.timeline({
        onComplete: () => {
          // Reset and restart after pause
          gsap.delayedCall(CYCLE_DURATION - ANIM_DURATION, () => {
            // Reset to origin
            nodes.forEach(n => {
              n.originX = Math.random() * w;
              n.originY = Math.random() * h;
              n.targetX = w * 0.3 + Math.random() * w * 0.4;
              n.targetY = h * 0.3 + Math.random() * h * 0.4;
              n.x = n.originX;
              n.y = n.originY;
              n.progress = 0;
            });
            connections.forEach(c => { c.progress = 0; c.dashOffset = 0; });
            animateCycle();
          });
        },
      });

      // Animate nodes drifting toward center & color shift
      nodes.forEach((node, i) => {
        node.x = node.originX;
        node.y = node.originY;
        node.progress = 0;
        tl.to(node, {
          x: node.targetX,
          y: node.targetY,
          progress: 1,
          duration: ANIM_DURATION,
          ease: 'power2.inOut',
        }, i * 0.05);
      });

      // Animate connections appearing
      connections.forEach((conn, i) => {
        conn.progress = 0;
        tl.to(conn, {
          progress: 1,
          duration: ANIM_DURATION * 0.6,
          ease: 'power1.in',
        }, ANIM_DURATION * 0.3 + i * 0.02);
      });

      // Render loop for this cycle
      const ticker = () => {
        connections.forEach(c => { c.dashOffset -= 0.3; });
        draw();
      };
      gsap.ticker.add(ticker);

      // Clean up ticker when cycle ends
      tl.eventCallback('onComplete', () => {
        gsap.ticker.remove(ticker);
        // Keep end state visible during pause, add a slow dash drift
        const pauseTicker = () => {
          connections.forEach(c => { c.dashOffset -= 0.1; });
          draw();
        };
        gsap.ticker.add(pauseTicker);
        gsap.delayedCall(CYCLE_DURATION - ANIM_DURATION, () => {
          gsap.ticker.remove(pauseTicker);
          // Reset and restart
          nodes.forEach(n => {
            n.originX = Math.random() * w;
            n.originY = Math.random() * h;
            n.targetX = w * 0.3 + Math.random() * w * 0.4;
            n.targetY = h * 0.3 + Math.random() * h * 0.4;
            n.x = n.originX;
            n.y = n.originY;
            n.progress = 0;
          });
          connections.forEach(c => { c.progress = 0; c.dashOffset = 0; });
          animateCycle();
        });
      });
    }

    resize();
    window.addEventListener('resize', () => {
      resize();
      createNodes();
    });
    createNodes();
    animateCycle();
  }

  /* ──────────────────────────────────────────────────────────
     2. GLOW CARDS — Mouse tracking
     ────────────────────────────────────────────────────────── */
  function initGlowCards() {
    document.addEventListener('mousemove', (e) => {
      document.querySelectorAll('.glow-card').forEach(card => {
        const rect = card.getBoundingClientRect();
        const x = e.clientX - rect.left;
        const y = e.clientY - rect.top;
        card.style.setProperty('--mouse-x', x + 'px');
        card.style.setProperty('--mouse-y', y + 'px');
      });
    });
  }

  /* ──────────────────────────────────────────────────────────
     3. GSAP BOOT SEQUENCE
     ────────────────────────────────────────────────────────── */
  function initBootSequence() {
    if (REDUCED_MOTION) return;

    const tl = gsap.timeline({ defaults: { ease: 'power2.out' } });

    // Nav fade in
    const nav = document.getElementById('main-nav');
    if (nav) {
      gsap.set(nav, { opacity: 0, y: -10 });
      tl.to(nav, { opacity: 1, y: 0, duration: 0.4 }, 0);
    }

    // Headline blur-in
    const headline = document.querySelector('.hero-title');
    if (headline) {
      gsap.set(headline, { opacity: 0, filter: 'blur(10px)' });
      tl.to(headline, { opacity: 1, filter: 'blur(0px)', duration: 0.6 }, 0.2);
    }

    // Hero subtitle
    const subtitle = document.querySelector('.hero-subtitle');
    if (subtitle) {
      gsap.set(subtitle, { opacity: 0, y: 10 });
      tl.to(subtitle, { opacity: 1, y: 0, duration: 0.4 }, 0.4);
    }

    // Targeting console
    const console = document.querySelector('.targeting-console');
    if (console) {
      gsap.set(console, { opacity: 0, y: 10 });
      tl.to(console, { opacity: 1, y: 0, duration: 0.4 }, 0.5);
    }
  }

  /* ──────────────────────────────────────────────────────────
     4. SCROLL REVEAL — IntersectionObserver
     ────────────────────────────────────────────────────────── */
  function initScrollReveal() {
    if (REDUCED_MOTION) {
      document.querySelectorAll('.reveal').forEach(el => el.classList.add('active'));
      return;
    }

    const observer = new IntersectionObserver(
      (entries) => {
        entries.forEach(entry => {
          if (entry.isIntersecting) {
            entry.target.classList.add('active');
            observer.unobserve(entry.target);
          }
        });
      },
      { threshold: 0.1, rootMargin: '0px 0px -40px 0px' }
    );

    document.querySelectorAll('.reveal').forEach(el => observer.observe(el));
  }

  /* ──────────────────────────────────────────────────────────
     5. LOADING BUTTONS
     ────────────────────────────────────────────────────────── */
  function initLoadingButtons() {
    document.querySelectorAll('form').forEach(form => {
      form.addEventListener('submit', () => {
        const btn = form.querySelector('.btn-primary, button[type="submit"]');
        if (btn && !btn.classList.contains('btn-loading')) {
          btn.classList.add('btn-loading');
          btn.disabled = true;
        }
      });
    });
  }

  /* ──────────────────────────────────────────────────────────
     6. TAB SYSTEM
     ────────────────────────────────────────────────────────── */
  function initTabs() {
    const tabBtns = document.querySelectorAll('.tab-btn');
    const tabContents = document.querySelectorAll('.tab-content');

    tabBtns.forEach(btn => {
      btn.addEventListener('click', () => {
        const tabId = btn.getAttribute('data-tab');

        tabBtns.forEach(b => b.classList.remove('active'));
        tabContents.forEach(c => c.classList.remove('active'));

        btn.classList.add('active');
        const target = document.getElementById(tabId);
        if (target) target.classList.add('active');

        // Trigger graph render when switching to graph tab
        if (tabId === 'tab-graph') {
          const subjectId = document.getElementById('subject-data')?.getAttribute('data-id');
          if (subjectId) renderGraph(subjectId);
        }
      });
    });
  }

  /* ──────────────────────────────────────────────────────────
     7. NOTES SIDEBAR NAVIGATION
     ────────────────────────────────────────────────────────── */
  function initNotesNav() {
    const noteItems = document.querySelectorAll('.note-sidebar-item');
    const noteBodies = document.querySelectorAll('.note-body-content');

    noteItems.forEach(item => {
      item.addEventListener('click', () => {
        const noteId = item.getAttribute('data-note-id');

        noteItems.forEach(i => i.classList.remove('active'));
        noteBodies.forEach(b => b.classList.remove('active'));

        item.classList.add('active');
        const target = document.getElementById(`note-${noteId}`);
        if (target) target.classList.add('active');
      });
    });
  }

  /* ──────────────────────────────────────────────────────────
     8. D3 KNOWLEDGE GRAPH
     ────────────────────────────────────────────────────────── */
  function renderGraph(subjectId) {
    const container = document.getElementById('entity-graph');
    if (!container) return;

    // Clear previous renders
    container.innerHTML = '';
    d3.selectAll('.graph-tooltip').remove();

    const colorMap = {
      person: '#FFAB00',       // signal
      place: '#2BD9CB',        // raw
      work: '#FF6D00',         // ember
      organization: '#847E72', // dim
      event: '#FFAB00',        // signal
    };

    const tooltip = d3.select('body').append('div')
      .attr('class', 'graph-tooltip')
      .style('opacity', 0);

    const width = container.clientWidth;
    const height = container.clientHeight || 600;

    const svg = d3.select('#entity-graph')
      .append('svg')
      .attr('width', '100%')
      .attr('height', '100%')
      .attr('viewBox', `0 0 ${width} ${height}`)
      .style('background-color', 'var(--void)');

    const g = svg.append('g');
    svg.call(d3.zoom().on('zoom', (event) => {
      g.attr('transform', event.transform);
    }));

    fetch(`/api/subjects/${subjectId}/graph-data`)
      .then(res => res.json())
      .then(data => {
        if (!data.nodes || data.nodes.length === 0) {
          container.innerHTML = '<div style="display:flex;align-items:center;justify-content:center;height:100%;color:var(--dim);font-family:Plus Jakarta Sans,sans-serif;font-size:14px;"><div style="text-align:center;"><iconify-icon icon="mdi:graph-outline" width="40" style="display:block;margin:0 auto 12px;opacity:0.4;"></iconify-icon>No graph data yet. Complete the NER stage.</div></div>';
          return;
        }

        const nodes = data.nodes;
        const links = data.links;

        const simulation = d3.forceSimulation(nodes)
          .force('link', d3.forceLink(links).id(d => d.id).distance(150))
          .force('charge', d3.forceManyBody().strength(-300))
          .force('center', d3.forceCenter(width / 2, height / 2))
          .force('collision', d3.forceCollide().radius(d => d.size + 10));

        // Links
        const link = g.append('g')
          .selectAll('line')
          .data(links)
          .join('line')
          .attr('stroke', d => {
            const opacity = d.confidence ? Math.max(0.06, d.confidence * 0.3) : 0.08;
            return `rgba(255, 171, 0, ${opacity})`;
          })
          .attr('stroke-width', 1.5);

        // Link labels
        const linkLabels = g.append('g')
          .selectAll('text')
          .data(links)
          .join('text')
          .attr('fill', '#847E72')
          .attr('font-size', '8px')
          .attr('font-family', 'JetBrains Mono, monospace')
          .attr('text-anchor', 'middle')
          .text(d => d.type);

        // Node groups
        const node = g.append('g')
          .selectAll('g')
          .data(nodes)
          .join('g')
          .call(d3.drag()
            .on('start', dragstarted)
            .on('drag', dragged)
            .on('end', dragended)
          );

        // Node circles
        node.append('circle')
          .attr('r', d => d.size)
          .attr('fill', d => d.is_subject ? '#FFAB00' : (colorMap[d.type] || '#847E72'))
          .attr('stroke', d => d.is_subject ? '#F3EFE4' : 'rgba(243, 239, 228, 0.15)')
          .attr('stroke-width', d => d.is_subject ? 3 : 1)
          .style('cursor', 'pointer')
          .on('mouseover', function (event, d) {
            d3.select(this)
              .transition()
              .duration(150)
              .attr('stroke-width', 3)
              .attr('stroke', '#F3EFE4')
              .style('filter', `drop-shadow(0 0 10px ${colorMap[d.type] || '#FFAB00'}80)`);

            tooltip.transition().duration(200).style('opacity', 0.95);
            tooltip.html(`
              <strong style="color:var(--bone);">${d.name}</strong><br/>
              <span style="font-family:JetBrains Mono,monospace;font-size:10px;text-transform:uppercase;color:${colorMap[d.type] || '#847E72'}">${d.type}</span>
              ${d.is_subject ? '<br/><em style="font-size:10px;color:var(--signal);">Central Subject</em>' : ''}
            `)
              .style('left', (event.pageX + 15) + 'px')
              .style('top', (event.pageY - 28) + 'px');
          })
          .on('mouseout', function (event, d) {
            d3.select(this)
              .transition()
              .duration(150)
              .attr('stroke-width', d.is_subject ? 3 : 1)
              .attr('stroke', d.is_subject ? '#F3EFE4' : 'rgba(243, 239, 228, 0.15)')
              .style('filter', 'none');

            tooltip.transition().duration(300).style('opacity', 0);
          })
          .on('click', function (event, d) {
            const connectedNodeIds = new Set([d.id]);
            links.forEach(l => {
              if (l.source.id === d.id) connectedNodeIds.add(l.target.id);
              if (l.target.id === d.id) connectedNodeIds.add(l.source.id);
            });

            node.selectAll('circle')
              .transition().duration(200)
              .style('opacity', n => connectedNodeIds.has(n.id) ? 1.0 : 0.12);
            node.selectAll('text')
              .transition().duration(200)
              .style('opacity', n => connectedNodeIds.has(n.id) ? 1.0 : 0.12);
            link.transition().duration(200)
              .style('opacity', l => (l.source.id === d.id || l.target.id === d.id) ? 1.0 : 0.04);

            event.stopPropagation();
          });

        // Node labels
        node.append('text')
          .attr('dx', d => d.size + 6)
          .attr('dy', '.35em')
          .attr('fill', '#F3EFE4')
          .attr('font-size', d => d.is_subject ? '12px' : '10px')
          .attr('font-weight', d => d.is_subject ? '600' : '400')
          .attr('font-family', 'Rajdhani, sans-serif')
          .text(d => d.name)
          .style('pointer-events', 'none');

        // Click background to reset
        svg.on('click', () => {
          node.selectAll('circle').transition().duration(200).style('opacity', 1.0);
          node.selectAll('text').transition().duration(200).style('opacity', 1.0);
          link.transition().duration(200).style('opacity', 1.0);
        });

        // Tick
        simulation.on('tick', () => {
          link
            .attr('x1', d => d.source.x)
            .attr('y1', d => d.source.y)
            .attr('x2', d => d.target.x)
            .attr('y2', d => d.target.y);

          linkLabels
            .attr('x', d => (d.source.x + d.target.x) / 2)
            .attr('y', d => (d.source.y + d.target.y) / 2 - 5);

          node.attr('transform', d => `translate(${d.x}, ${d.y})`);
        });

        function dragstarted(event, d) {
          if (!event.active) simulation.alphaTarget(0.3).restart();
          d.fx = d.x;
          d.fy = d.y;
        }

        function dragged(event, d) {
          d.fx = event.x;
          d.fy = event.y;
        }

        function dragended(event, d) {
          if (!event.active) simulation.alphaTarget(0);
          d.fx = null;
          d.fy = null;
        }
      })
      .catch(err => {
        console.error('Graph data fetch error:', err);
        container.innerHTML = '<div style="display:flex;align-items:center;justify-content:center;height:100%;color:var(--alert);font-size:13px;">Failed to load graph data.</div>';
      });
  }

  /* ──────────────────────────────────────────────────────────
     9. PIPELINE STATUS TIMERS & POLLING
     ────────────────────────────────────────────────────────── */
  let timerInterval = null;

  function formatDuration(seconds) {
    if (seconds < 0) return '0s';
    if (seconds < 60) return `${seconds}s`;
    const mins = Math.floor(seconds / 60);
    const secs = seconds % 60;
    return `${mins}m ${secs}s`;
  }

  function runTimerTick() {
    let runningStageNode = null;
    let anyRunning = false;

    document.querySelectorAll('.step-node').forEach(node => {
      const statusClass = Array.from(node.classList).find(c => ['complete', 'failed', 'running', 'skipped', 'pending'].includes(c));
      const startedStr = node.getAttribute('data-started');
      const completedStr = node.getAttribute('data-completed');
      const timerElement = node.querySelector('.step-timer');

      if (!timerElement) return;

      if (statusClass === 'running') {
        runningStageNode = node;
        anyRunning = true;
      }

      if (startedStr) {
        const start = new Date(startedStr);
        const end = (completedStr && statusClass !== 'running') ? new Date(completedStr) : new Date();
        const durationSec = Math.max(0, Math.floor((end - start) / 1000));
        
        if (statusClass === 'complete') {
          timerElement.textContent = `Took ${formatDuration(durationSec)}`;
          timerElement.style.color = 'var(--dim)';
        } else if (statusClass === 'failed') {
          timerElement.textContent = `Failed after ${formatDuration(durationSec)}`;
          timerElement.style.color = 'var(--alert)';
        } else if (statusClass === 'skipped') {
          timerElement.textContent = 'Skipped';
          timerElement.style.color = 'var(--dim)';
        } else if (statusClass === 'running') {
          timerElement.textContent = `Running: ${formatDuration(durationSec)}`;
          timerElement.style.color = 'var(--raw)';
        } else {
          timerElement.textContent = '';
        }
      } else {
        if (statusClass === 'skipped') {
          timerElement.textContent = 'Skipped';
          timerElement.style.color = 'var(--dim)';
        } else {
          timerElement.textContent = '';
        }
      }
    });

    const activeInfo = document.getElementById('active-step-info');
    if (activeInfo) {
      if (anyRunning && runningStageNode) {
        const stageName = runningStageNode.getAttribute('data-stage');
        const startedStr = runningStageNode.getAttribute('data-started');
        if (startedStr) {
          const start = new Date(startedStr);
          const durationSec = Math.max(0, Math.floor((new Date() - start) / 1000));
          document.getElementById('active-step-name').textContent = stageName;
          document.getElementById('active-step-duration').textContent = formatDuration(durationSec);
          activeInfo.style.display = 'flex';
        } else {
          activeInfo.style.display = 'none';
        }
      } else {
        activeInfo.style.display = 'none';
      }
    }
  }

  function startGlobalTimers() {
    if (timerInterval) clearInterval(timerInterval);
    runTimerTick();
    timerInterval = setInterval(runTimerTick, 1000);
  }

  function pollStatus(subjectId) {
    const interval = setInterval(() => {
      fetch(`/api/status/${subjectId}`)
        .then(res => res.json())
        .then(data => {
          let anyRunning = false;

          // Update stepper nodes
          data.stages.forEach((stage, idx) => {
            const stepNode = document.querySelector(`.step-node[data-stage="${stage.stage}"]`);
            if (stepNode) {
              stepNode.className = `step-node ${stage.status}`;
              
              if (stage.started_at) {
                stepNode.setAttribute('data-started', stage.started_at);
              } else {
                stepNode.removeAttribute('data-started');
              }
              if (stage.completed_at) {
                stepNode.setAttribute('data-completed', stage.completed_at);
              } else {
                stepNode.removeAttribute('data-completed');
              }

              const dot = stepNode.querySelector('.step-dot');
              if (dot) {
                if (stage.status === 'complete') {
                  dot.innerHTML = '<iconify-icon icon="mdi:check" width="14"></iconify-icon>';
                } else if (stage.status === 'failed') {
                  dot.innerHTML = '<iconify-icon icon="mdi:close" width="14"></iconify-icon>';
                } else if (stage.status === 'running') {
                  dot.innerHTML = '<iconify-icon icon="mdi:loading" width="14" style="animation: spin 1s linear infinite;"></iconify-icon>';
                } else {
                  dot.textContent = idx + 1;
                }
              }
            }
            if (stage.status === 'running' || stage.status === 'pending') {
              anyRunning = true;
            }
          });

          // Run timer update immediately
          runTimerTick();

          // Update stats
          const docVal = document.getElementById('stat-docs');
          const chunkVal = document.getElementById('stat-chunks');
          const entVal = document.getElementById('stat-entities');
          const noteVal = document.getElementById('stat-notes');

          if (docVal && data.stats.documents != null) docVal.textContent = data.stats.documents;
          if (chunkVal && data.stats.chunks != null) chunkVal.textContent = data.stats.chunks;
          if (entVal && data.stats.entities != null) entVal.textContent = data.stats.entities;
          if (noteVal && data.stats.notes != null) noteVal.textContent = data.stats.notes;

          if (!anyRunning) {
            clearInterval(interval);
            if (timerInterval) clearInterval(timerInterval);
            showToast('Pipeline completed! Reloading...', 'success');
            setTimeout(() => window.location.reload(), 1500);
          }
        })
        .catch(err => {
          console.error('Polling error:', err);
          clearInterval(interval);
        });
    }, 3000);
  }

  /* ──────────────────────────────────────────────────────────
     10. EXPORT VAULT
     ────────────────────────────────────────────────────────── */
  window.exportVault = function (subjectId) {
    const btn = document.getElementById('export-btn');
    if (btn) btn.classList.add('btn-loading');

    showToast('Exporting vault to Obsidian...', 'info');
    fetch(`/subjects/${subjectId}/export`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
    })
      .then(res => res.json())
      .then(data => {
        if (btn) btn.classList.remove('btn-loading');
        if (data.status === 'success') {
          showToast(`Vault exported: ${data.vault_path}`, 'success');
        } else {
          showToast(`Export failed: ${data.message}`, 'error');
        }
      })
      .catch(err => {
        if (btn) btn.classList.remove('btn-loading');
        console.error('Export error:', err);
        showToast('Server error during vault export.', 'error');
      });
  };

  /* ──────────────────────────────────────────────────────────
     11. TOAST NOTIFICATIONS
     ────────────────────────────────────────────────────────── */
  window.showToast = function (message, type = 'success') {
    const container = document.getElementById('toast-container');
    if (!container) return;

    const icons = {
      success: 'mdi:check-circle-outline',
      error: 'mdi:alert-circle-outline',
      info: 'mdi:information-outline',
    };

    const toast = document.createElement('div');
    toast.className = `toast ${type}`;
    toast.innerHTML = `
      <iconify-icon icon="${icons[type] || icons.info}" width="16"></iconify-icon>
      <span>${message}</span>
    `;
    container.appendChild(toast);

    setTimeout(() => {
      toast.style.opacity = '0';
      toast.style.transform = 'translateX(20px)';
      setTimeout(() => toast.remove(), 300);
    }, 4000);
  };

  /* ──────────────────────────────────────────────────────────
     12. PDF UPLOAD ZONE
     ────────────────────────────────────────────────────────── */
  function initUploadZone() {
    const zone = document.getElementById('upload-zone');
    const fileInput = document.getElementById('pdf-file-input');
    const fileNameDisplay = document.getElementById('pdf-file-name');
    const uploadBtn = document.getElementById('pdf-upload-btn');
    const form = document.getElementById('pdf-upload-form');

    if (!zone || !fileInput) return;

    // Click to browse
    zone.addEventListener('click', () => fileInput.click());

    // Drag & drop
    zone.addEventListener('dragover', (e) => {
      e.preventDefault();
      zone.classList.add('drag-over');
    });
    zone.addEventListener('dragleave', () => {
      zone.classList.remove('drag-over');
    });
    zone.addEventListener('drop', (e) => {
      e.preventDefault();
      zone.classList.remove('drag-over');
      if (e.dataTransfer.files.length) {
        fileInput.files = e.dataTransfer.files;
        onFileSelected();
      }
    });

    // File selected
    fileInput.addEventListener('change', onFileSelected);

    function onFileSelected() {
      if (fileInput.files.length > 0) {
        const name = fileInput.files[0].name;
        if (fileNameDisplay) {
          fileNameDisplay.textContent = `Selected: ${name}`;
          fileNameDisplay.style.display = 'block';
        }
        if (uploadBtn) uploadBtn.style.display = 'inline-flex';
      }
    }

    // Form submit loading
    if (form) {
      form.addEventListener('submit', () => {
        if (uploadBtn) {
          uploadBtn.classList.add('btn-loading');
          uploadBtn.disabled = true;
        }
      });
    }
  }

  /* ──────────────────────────────────────────────────────────
     13. SCRAPER CONTROL PANEL
     ────────────────────────────────────────────────────────── */
  function initScraperPanel() {
    const slider = document.getElementById('max-sources-slider');
    const valueDisplay = document.getElementById('max-sources-value');
    const saveBtn = document.getElementById('save-scrape-config');

    if (slider && valueDisplay) {
      slider.addEventListener('input', () => {
        valueDisplay.textContent = slider.value;
      });
    }

    if (saveBtn) {
      saveBtn.addEventListener('click', () => {
        const config = {};
        document.querySelectorAll('.scraper-toggle-row input[type="checkbox"]').forEach(cb => {
          config[cb.dataset.source] = cb.checked;
        });
        config.max_sources = slider ? parseInt(slider.value) : 5;

        saveBtn.classList.add('btn-loading');
        saveBtn.disabled = true;

        fetch('/api/scrape-config', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(config),
        })
          .then(res => res.json())
          .then(() => {
            showToast('Scraper configuration saved', 'success');
            saveBtn.classList.remove('btn-loading');
            saveBtn.disabled = false;
          })
          .catch(() => {
            showToast('Failed to save configuration', 'error');
            saveBtn.classList.remove('btn-loading');
            saveBtn.disabled = false;
          });

        // Update status dots
        document.querySelectorAll('.scraper-toggle-row').forEach(row => {
          const cb = row.querySelector('input[type="checkbox"]');
          const dot = row.querySelector('.scraper-status-dot');
          if (cb && dot) {
            dot.className = 'scraper-status-dot ' + (cb.checked ? 'active' : 'disabled');
          }
        });
      });
    }

    // Toggle status dots on checkbox change
    document.querySelectorAll('.scraper-toggle-row input[type="checkbox"]').forEach(cb => {
      cb.addEventListener('change', () => {
        const dot = cb.closest('.scraper-toggle-row').querySelector('.scraper-status-dot');
        if (dot) {
          dot.className = 'scraper-status-dot ' + (cb.checked ? 'active' : 'disabled');
        }
      });
    });
  }

  /* ──────────────────────────────────────────────────────────
     INIT — DOMContentLoaded
     ────────────────────────────────────────────────────────── */
  document.addEventListener('DOMContentLoaded', () => {
    // Core systems
    initConstellation();
    initGlowCards();
    initBootSequence();
    initScrollReveal();
    initLoadingButtons();
    initTabs();
    initNotesNav();
    initUploadZone();
    initScraperPanel();

    // Start UI timers
    startGlobalTimers();

    // Pipeline status polling
    const subjectData = document.getElementById('subject-data');
    if (subjectData) {
      const id = subjectData.getAttribute('data-id');
      const isRunning = subjectData.getAttribute('data-running') === 'true';
      if (isRunning) {
        pollStatus(id);
      }
    }
  });

})();
