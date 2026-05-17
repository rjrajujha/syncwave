    (function () {
      const PIN_REQUIRED = {{PIN_REQUIRED}};
      const WS_PATH = '{{WS_PATH}}';
      const APP_VERSION = '{{APP_VERSION}}';
      const ROOM_DEFAULT_PREFIX = '{{ROOM_DEFAULT_PREFIX}}';
      const IS_WAN = WS_PATH === '/ws';

      const roomInput = document.getElementById('roomInput');
      const pinInput = document.getElementById('pinInput');
      const connectBtn = document.getElementById('connectBtn');
      const disconnectBtn = document.getElementById('disconnectBtn');
      const toggleBtn = document.getElementById('toggleBtn');
      const bufferValue = document.getElementById('bufferValue');
      const latencyValue = document.getElementById('latencyValue');
      const offsetValue = document.getElementById('offsetValue');
      const chunkRx = document.getElementById('chunkRx');
      const chunkDrop = document.getElementById('chunkDrop');
      const audioCtxState = document.getElementById('audioCtxState');
      const lastRms = document.getElementById('lastRms');
      const modalRtt = document.getElementById('modalRtt');
      const modalOffset = document.getElementById('modalOffset');
      const statusBadge = document.getElementById('statusBadge');
      const statusText = document.getElementById('statusText');
      const errorText = document.getElementById('errorText');
      const headerListenerWrap = document.getElementById('headerListenerWrap');
      const headerListenerCount = document.getElementById('headerListenerCount');
      const roomTitle = document.getElementById('roomTitle');
      const roomSubtitle = document.getElementById('roomSubtitle');
      const infoBtn = document.getElementById('infoBtn');
      const infoModal = document.getElementById('infoModal');
      const closeInfo = document.getElementById('closeInfo');
      const detailsBtn = document.getElementById('detailsBtn');
      const detailsModal = document.getElementById('detailsModal');
      const closeDetails = document.getElementById('closeDetails');
      const waveCanvas = document.getElementById('waveCanvas');

      const ROOM_CODE_PATTERN = /^(LAN|WAN|SW)-[A-Z0-9]{2,8}(-[A-Z0-9]{1,4})?$/;

      let ws = null;
      let activeRoom = '';
      let audioCtx = null;
      let gainNode = null;
      let pingTimer = null;
      let scheduleTimer = null;
      let latencyRefreshTimer = null;
      let nextPlayTime = 0;
      const DEFAULT_TARGET_BUFFER_MS = 680;
      const MIN_OFFSET_SAMPLES = 6;
      // Slightly relaxed startup lead — output-latency compensation lets us
      // start sooner without sacrificing inter-device alignment.
      const STARTUP_MIN_HEAD_LEAD_MS = 180;
      const STEADY_STALE_LATE_MS = 520;
      const SCHEDULE_INTERVAL_MS = 10;
      // Soft underrun budget. We tolerate small audio-clock slips (e.g. a tab
      // throttled by a backgrounded GC pause) without resetting the queue —
      // a full rebuffer is reserved for catastrophic slips.
      const SOFT_UNDERRUN_MS = 220;
      const HARD_UNDERRUN_MS = 520;
      let targetBufferMs = DEFAULT_TARGET_BUFFER_MS;
      let maxBufferMs = 900;
      let maxQueueMs = 1680;
      let minStartupQueueMs = 300;
      let queue = [];
      let queuedMs = 0;
      let started = false;
      let pausedByUser = false;
      let lastSequence = null;
      // Tracks the last *received* chunk's playAt so seq-gap silence fills
      // carry an inferred playAt and the chain stays continuous.
      let lastReceivedPlayAtMs = null;
      let nominalChunkDurationMs = 40;
      // playAt uses ping/pong offset; scheduling is shared across listeners
      // (see wallAligned lead formula in schedulePlayback). Each schedule call
      // also compensates for the device-local audio pipeline delay so the
      // *audible* moment lines up with the synchronized wall clock.
      let clockOffsetSamples = [];
      let clockOffsetMs = 0;
      let lastRttMs = 0;
      let offsetSpanMs = 0;
      let syncPhase = 'idle';
      let chunksReceived = 0;
      let chunksDroppedDup = 0;
      let useBinaryLanTransport = false;
      let lastListenerCount = 0;
      let rmsSmoothed = 0;
      let waveAnim = 0;
      let listenerSessionStartPerf = null;
      let firstPlaybackWallPerf = null;
      // Smoothed audio-pipeline latency (baseLatency + outputLatency) — both
      // may report 0 until the device starts mixing; we keep an EMA so we
      // converge once the platform provides real values.
      let pipelineLatencySec = 0;
      // The last rate we applied — used to ramp gently between adjacent
      // chunks so rate changes never sound like a sudden pitch step.
      let lastAppliedRate = 1;

      function isJoinWarmupWindow() {
        return listenerSessionStartPerf != null &&
          performance.now() - listenerSessionStartPerf < 3200;
      }

      function isPlaybackWarmupWindow() {
        return firstPlaybackWallPerf != null &&
          performance.now() - firstPlaybackWallPerf < 3200;
      }

      function localWallNowMs() {
        if (typeof performance !== 'undefined' &&
            typeof performance.now === 'function') {
          const origin = Number(performance.timeOrigin || 0);
          if (origin > 0) return origin + performance.now();
        }
        return Date.now();
      }

      function estimatedServerNowMs() {
        return localWallNowMs() + clockOffsetMs;
      }

      function refreshPipelineLatency() {
        if (!audioCtx) return;
        const base = Number(audioCtx.baseLatency || 0);
        const out = Number(audioCtx.outputLatency || 0);
        // outputLatency is the more reliable signal across Chrome/Firefox;
        // Safari historically only reports baseLatency. baseLatency is the
        // audio-graph internal buffer; outputLatency is the OS/hardware
        // buffer. We compensate for both so the *audible* moment, not the
        // schedule moment, hits the shared wall-clock target.
        let next = 0;
        if (out > 0) next = out + Math.max(0, base);
        else if (base > 0) next = base;
        // Clamp to sane bounds; some devices misreport huge values once.
        if (next > 0.5) next = 0.5;
        if (next < 0) next = 0;
        if (pipelineLatencySec === 0) {
          pipelineLatencySec = next;
        } else {
          // EMA: react quickly during warmup, slowly in steady-state so a
          // single misreport can't yank scheduling.
          const w = isJoinWarmupWindow() || isPlaybackWarmupWindow() ? 0.5 : 0.12;
          pipelineLatencySec = pipelineLatencySec * (1 - w) + next * w;
        }
      }

      const urlParams =
        typeof location !== 'undefined'
          ? new URLSearchParams(location.search)
          : null;
      const SYNC_DEBUG = urlParams?.get('syncDebug') === '1';
      const SYNC_TELEMETRY = urlParams?.get('syncTelemetry') === '1';
      function recordSyncMetrics(
        scheduleAt,
        wallAligned,
        chainHead,
        leadMs,
        queuedAfter,
        correctionMs,
        playbackRate,
      ) {
        if (!SYNC_TELEMETRY) return;
        if (!window.__swSyncTelemetry) {
          window.__swSyncTelemetry = { events: [], max: 160 };
        }
        const buf = window.__swSyncTelemetry;
        buf.events.push({
          t: Math.round(performance.now()),
          leadMs: Math.round(leadMs),
          schedMinusWallMs: Math.round((scheduleAt - wallAligned) * 1000),
          schedMinusChainMs: Math.round((scheduleAt - chainHead) * 1000),
          queuedMs: Math.round(queuedAfter),
          offsetMs: Math.round(clockOffsetMs),
          rttMs: Math.round(lastRttMs),
          offsetSpanMs: Math.round(offsetSpanMs),
          correctionMs: Math.round(correctionMs),
          pipelineMs: Math.round(pipelineLatencySec * 1000),
          rate: Number(playbackRate.toFixed(4)),
          phase: syncPhase,
        });
        if (buf.events.length > buf.max) buf.events.shift();
      }

      const syncProbe =
        SYNC_DEBUG
          ? { n: 0, sumWallMinusChainMs: 0, last: [] }
          : null;
      function recordSyncProbe(wallAligned, chainHead) {
        if (!syncProbe) return;
        const d = (wallAligned - chainHead) * 1000;
        syncProbe.n++;
        syncProbe.sumWallMinusChainMs += d;
        syncProbe.last.push(Math.round(d));
        if (syncProbe.last.length > 64) syncProbe.last.shift();
        window.__swSyncProbe = syncProbe;
      }

      function setTargetBuffer(value) {
        const next = Number(value || DEFAULT_TARGET_BUFFER_MS);
        targetBufferMs = Math.max(520, Math.min(1100, next));
        // Scheduling horizon now slightly *exceeds* the target buffer so the
        // chunks we already hold can all be handed to Web Audio. This keeps
        // sample-accurate scheduling decisions on the audio thread instead
        // of competing with the JS timer cadence.
        maxBufferMs = Math.max(targetBufferMs + 160, targetBufferMs * 1.25);
        maxQueueMs = Math.max(1400, Math.min(2400, targetBufferMs + 1000));
        minStartupQueueMs = Math.max(240, Math.min(420, targetBufferMs - 380));
      }

      function closeDetailsIfOpen() {
        try {
          if (detailsModal && detailsModal.open) detailsModal.close();
        } catch (_) {}
      }

      function startWaveRenderer() {
        const canvas = waveCanvas;
        if (!canvas) return;
        const ctx = canvas.getContext('2d');
        if (!ctx) return;
        function resize() {
          const rect = canvas.getBoundingClientRect();
          const dpr = window.devicePixelRatio || 1;
          const rw = Math.max(1, rect.width);
          const rh = Math.max(1, rect.height);
          canvas.width = Math.floor(rw * dpr);
          canvas.height = Math.floor(rh * dpr);
          ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
        }
        function tick() {
          resize();
          const rect = canvas.getBoundingClientRect();
          const w = Math.max(1, rect.width);
          const h = Math.max(1, rect.height);
          ctx.clearRect(0, 0, w, h);
          waveAnim += 0.028 + Math.min(0.22, rmsSmoothed) * 2.4;
          const mid = h * 0.5;
          const layers = 4;
          for (let L = 0; L < layers; L++) {
            ctx.beginPath();
            const amp = (8 + L * 6) * (0.4 + Math.min(0.9, rmsSmoothed * 5));
            const phase = waveAnim + L * 0.55;
            for (let x = 0; x <= w + 0.5; x += 3) {
              const t = (x / w) * Math.PI * 2;
              const y = mid + Math.sin(t * 2.2 + phase) * amp * 0.52 +
                Math.sin(t * 5.1 + phase * 0.85) * amp * 0.22;
              if (x <= 0.5) ctx.moveTo(x, y);
              else ctx.lineTo(x, y);
            }
            const grd = ctx.createLinearGradient(0, 0, w, 0);
            grd.addColorStop(0, 'rgba(34, 211, 238, 0.55)');
            grd.addColorStop(1, 'rgba(251, 146, 60, 0.55)');
            ctx.strokeStyle = grd;
            ctx.globalAlpha = 0.28 + (layers - L) * 0.11;
            ctx.lineWidth = 2;
            ctx.stroke();
          }
          ctx.globalAlpha = 1;
          rmsSmoothed *= 0.94;
          requestAnimationFrame(tick);
        }
        window.addEventListener('resize', resize);
        requestAnimationFrame(tick);
      }
      startWaveRenderer();

      function chunkEventFromMessage(decoded) {
        const inner = decoded.payload;
        if (inner && typeof inner === 'object' && !Array.isArray(inner)) {
          return inner;
        }
        return decoded;
      }

      function setStatus(label, css) {
        statusText.textContent = label;
        statusBadge.className = 'status-pill ' + css;
      }

      function setError(message) {
        errorText.textContent = message || '';
      }

      function validateRoomCode(value) {
        return ROOM_CODE_PATTERN.test(value);
      }

      function ensureAudio() {
        if (!audioCtx) {
          audioCtx = new (window.AudioContext || window.webkitAudioContext)({ sampleRate: 48000 });
        }
        if (!gainNode) {
          gainNode = audioCtx.createGain();
          gainNode.gain.value = pausedByUser ? 0 : 1;
          gainNode.connect(audioCtx.destination);
        }
        if (!scheduleTimer) {
          scheduleTimer = setInterval(schedulePlayback, SCHEDULE_INTERVAL_MS);
        }
        if (!latencyRefreshTimer) {
          // Refresh pipeline latency periodically — devices can change output
          // routes (e.g. Bluetooth) mid-session.
          refreshPipelineLatency();
          latencyRefreshTimer = setInterval(refreshPipelineLatency, 1500);
        }
      }

      function resetPlayback() {
        queue = [];
        queuedMs = 0;
        started = false;
        pausedByUser = false;
        lastSequence = null;
        lastReceivedPlayAtMs = null;
        nextPlayTime = audioCtx ? audioCtx.currentTime + 0.12 : 0;
        chunksReceived = 0;
        chunksDroppedDup = 0;
        chunkRx.textContent = '0';
        chunkDrop.textContent = '0';
        lastRms.textContent = '—';
        rmsSmoothed = 0;
        listenerSessionStartPerf = null;
        firstPlaybackWallPerf = null;
        clockOffsetSamples = [];
        clockOffsetMs = 0;
        lastRttMs = 0;
        offsetSpanMs = 0;
        syncPhase = 'warming';
        lastAppliedRate = 1;
        if (gainNode) gainNode.gain.value = 1;
        updateBufferLabel();
      }

      function updateBufferLabel() {
        bufferValue.textContent = Math.round(bufferLeadMs()) + ' ms';
        if (audioCtx) {
          audioCtxState.textContent = audioCtx.state;
        }
      }

      function bufferLeadMs() {
        const aheadMs = audioCtx
          ? Math.max(0, (nextPlayTime - audioCtx.currentTime) * 1000)
          : 0;
        return Math.max(0, aheadMs + queuedMs);
      }

      function readU64LE(dv, offset) {
        const lo = dv.getUint32(offset, true);
        const hi = dv.getUint32(offset + 4, true);
        return lo + hi * 0x100000000;
      }

      function u8ToB64(u8) {
        let binary = '';
        const chunk = 0x8000;
        for (let i = 0; i < u8.length; i += chunk) {
          binary += String.fromCharCode.apply(null, u8.subarray(i, i + chunk));
        }
        return btoa(binary);
      }

      function handleBinaryLanFrame(arrayBuffer) {
        const v = new DataView(arrayBuffer);
        if (arrayBuffer.byteLength < 32) return;
        if (v.getUint8(0) !== 0x53 || v.getUint8(1) !== 0x57 || v.getUint8(2) !== 0x41 || v.getUint8(3) !== 0x32) return;
        if (v.getUint8(4) !== 1) return;
        const flags = v.getUint8(5);
        let o = 8;
        const sequence = v.getUint32(o, true); o += 4;
        const playAtRaw = readU64LE(v, o); o += 8;
        const sampleRate = v.getUint32(o, true); o += 4;
        const channelCount = v.getUint16(o, true); o += 2;
        const durationMs = v.getUint16(o, true); o += 2;
        const pcmLen = v.getUint32(o, true);
        if (32 + pcmLen !== arrayBuffer.byteLength) return;
        const pcmBytes = new Uint8Array(arrayBuffer, 32, pcmLen);
        const hasPlayAt = (flags & 1) !== 0;
        enqueueChunk({
          sequence,
          sampleRate,
          channelCount,
          durationMs,
          payload: u8ToB64(pcmBytes),
          playAt: hasPlayAt ? playAtRaw : 0
        });
      }

      function toFloat32FromPcm16(payload) {
        const binary = atob(payload);
        const bytes = new Uint8Array(binary.length);
        for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i) & 0xff;
        const dv = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
        const out = new Float32Array(bytes.length / 2);
        for (let i = 0; i < out.length; i++) {
          out[i] = dv.getInt16(i * 2, true) / 32768;
        }
        return out;
      }

      function enqueueSilence(sampleRate, channelCount, durationMs, playAtServerMs) {
        if (!audioCtx) return;
        const frames = Math.max(1, Math.round(sampleRate * durationMs / 1000));
        const buf = audioCtx.createBuffer(Math.max(1, channelCount), frames, sampleRate);
        queue.push({
          buffer: buf,
          durationMs,
          playAtServerMs: typeof playAtServerMs === 'number' && playAtServerMs > 0
            ? playAtServerMs
            : null,
          isSilence: true,
        });
        queuedMs += durationMs;
      }

      function dropQueueHead() {
        const dropped = queue.shift();
        if (!dropped) return false;
        queuedMs = Math.max(0, queuedMs - (dropped.durationMs || 0));
        return true;
      }

      function trimQueuedAudioForSync(mode) {
        if (clockOffsetSamples.length < MIN_OFFSET_SAMPLES || queue.length <= 1) {
          return 0;
        }
        let removed = 0;
        const serverNow = estimatedServerNowMs();
        while (queue.length > 1) {
          const head = queue[0];
          const headPlayAt = Number(head?.playAtServerMs || 0);
          if (!headPlayAt) break;
          const headLeadMs = headPlayAt - serverNow;
          const shouldTrim = mode === 'startup'
            ? headLeadMs < STARTUP_MIN_HEAD_LEAD_MS
            : headLeadMs < -STEADY_STALE_LATE_MS && queuedMs > targetBufferMs;
          if (!shouldTrim) break;
          if (!dropQueueHead()) break;
          removed++;
        }
        return removed;
      }

      function regulateQueuedAudio() {
        trimQueuedAudioForSync(started ? 'steady' : 'startup');
        while (queue.length > 1 && queuedMs > maxQueueMs) {
          const head = queue[0];
          const headPlayAt = Number(head?.playAtServerMs || 0);
          if (started &&
              headPlayAt &&
              clockOffsetSamples.length >= MIN_OFFSET_SAMPLES &&
              headPlayAt - estimatedServerNowMs() > -220) {
            break;
          }
          if (!dropQueueHead()) break;
        }
      }

      function overlapCorrectionMs(errorMs, durationMs) {
        // Caller passes a positive errorMs ("we are scheduled this many ms
        // ahead of the wall target"). We respond by overlapping the next
        // chunk slightly so the chain pulls back toward the target.
        if (errorMs <= 8) return 0;
        const warm = isPlaybackWarmupWindow() || isJoinWarmupWindow();
        const cap = Math.min(durationMs * 0.18, warm ? 7.5 : 5.5);
        const response = warm ? 0.20 : 0.13;
        return Math.max(0, Math.min(cap, errorMs * response));
      }

      function rateForResidualBidirectional(residualMs) {
        // residualMs: chainHead - wallAligned, after correction.
        // > 0 → chunk is being played *ahead* of the wall target; rate > 1
        //       so the chunk plays faster, advancing chainHead by *less*
        //       wall time per chunk and letting wallAligned catch up.
        // < 0 → chunk is being played *behind* the wall target (a gap
        //       formed); rate < 1 so the chunk plays slower, advancing
        //       chainHead by *more* wall time per chunk and chunking the
        //       gap closed gradually without an audible jolt.
        const absMs = Math.abs(residualMs);
        if (absMs <= 16) return 1.0;
        const warm = isPlaybackWarmupWindow() || isJoinWarmupWindow();
        // Steady-state rate band is intentionally narrow (~0.5%) so the
        // pitch stays musically transparent; warmup may flex further to
        // converge faster.
        const maxDelta = warm ? 0.014 : 0.0045;
        const delta = Math.max(
          -maxDelta,
          Math.min(maxDelta, residualMs / 20000),
        );
        return 1.0 + delta;
      }

      function applyRateRamp(sourceParam, scheduleAt, targetRate) {
        // Sample-accurate ramp between chunks — longer in steady state so
        // drift correction stays musically transparent.
        if (!sourceParam) return;
        const startSec = Math.max(scheduleAt, audioCtx.currentTime);
        const nearUnity =
          Math.abs(targetRate - 1) < 0.0008 &&
          Math.abs(lastAppliedRate - 1) < 0.0008;
        const rampSec = nearUnity
          ? 0
          : syncPhase === 'locked'
          ? 0.036
          : 0.032;
        try {
          if (nearUnity) {
            sourceParam.cancelScheduledValues(startSec);
            sourceParam.setValueAtTime(1, startSec);
          } else {
            sourceParam.cancelScheduledValues(startSec);
            sourceParam.setValueAtTime(lastAppliedRate, startSec);
            if (Math.abs(targetRate - lastAppliedRate) > 1e-5) {
              sourceParam.linearRampToValueAtTime(
                targetRate,
                startSec + rampSec,
              );
            } else {
              sourceParam.setValueAtTime(targetRate, startSec + rampSec);
            }
          }
        } catch (_) {
          sourceParam.value = targetRate;
        }
        lastAppliedRate = targetRate;
      }

      function enqueueChunk(event) {
        const sampleRate = Number(event.sampleRate || 48000);
        const channelCount = Number(event.channelCount || 1);
        const durationMs = Number(event.durationMs || 0);
        if (!event.payload) return;

        chunksReceived++;
        chunkRx.textContent = String(chunksReceived);
        const seq = Number(event.sequence || 0);
        if (lastSequence !== null && seq <= lastSequence) {
          chunksDroppedDup++;
          chunkDrop.textContent = String(chunksDroppedDup);
          return;
        }

        const pcm = toFloat32FromPcm16(event.payload);
        let sumSq = 0;
        const step = Math.max(1, Math.floor(pcm.length / 256));
        for (let i = 0; i < pcm.length; i += step) sumSq += pcm[i] * pcm[i];
        const rms = Math.sqrt(sumSq / Math.ceil(pcm.length / step));
        lastRms.textContent = rms.toFixed(4);
        rmsSmoothed = Math.min(0.28, rmsSmoothed * 0.88 + rms * 0.12);
        const frames = channelCount > 0 ? Math.floor(pcm.length / channelCount) : pcm.length;
        const buffer = audioCtx.createBuffer(channelCount, frames, sampleRate);
        if (channelCount === 1) {
          buffer.copyToChannel(pcm, 0);
        } else {
          for (let c = 0; c < channelCount; c++) {
            const data = buffer.getChannelData(c);
            for (let i = 0; i < frames; i++) data[i] = pcm[i * channelCount + c] || 0;
          }
        }
        const chunkMs = durationMs > 0 ? durationMs : Math.round(frames / sampleRate * 1000);
        const incomingPlayAt = Number(event.playAt || 0) || null;

        if (lastSequence !== null && seq > lastSequence + 1) {
          const missing = seq - lastSequence - 1;
          const fill = Math.min(missing, 6);
          // Synthesise silence chunks whose playAt is interpolated from the
          // last *known* playAt, so the chain stays continuous and the next
          // real chunk doesn't have to absorb the full gap as a sync error.
          const fillDur = nominalChunkDurationMs || chunkMs || 40;
          const baselinePlayAt = lastReceivedPlayAtMs;
          for (let i = 0; i < fill; i++) {
            const inferredPlayAt = baselinePlayAt != null
              ? baselinePlayAt + (i + 1) * fillDur
              : null;
            enqueueSilence(sampleRate, channelCount, fillDur, inferredPlayAt);
          }
          if (missing > 6) {
            // Catastrophic gap — fall back to rebuffer.
            started = false;
            nextPlayTime = Math.max(audioCtx.currentTime + 0.16, nextPlayTime);
          }
        }
        lastSequence = seq;
        nominalChunkDurationMs = chunkMs > 0 ? chunkMs : nominalChunkDurationMs;
        if (incomingPlayAt) lastReceivedPlayAtMs = incomingPlayAt;

        queue.push({
          buffer,
          durationMs: chunkMs,
          playAtServerMs: incomingPlayAt,
          isSilence: false,
        });
        queuedMs += chunkMs;
        regulateQueuedAudio();
        if (audioCtx && audioCtx.state !== 'running') {
          audioCtx.resume().catch(() => {});
        }
      }

      function schedulePlayback() {
        if (!audioCtx || !gainNode || queue.length === 0) {
          updateBufferLabel();
          return;
        }
        if (!started) {
          syncPhase = 'warming';
          if (clockOffsetSamples.length < MIN_OFFSET_SAMPLES) {
            if (!pausedByUser) setStatus('Buffering', 'buffering');
            return;
          }
          trimQueuedAudioForSync('startup');
          const head = queue[0];
          const headPlayAt = Number(head?.playAtServerMs || 0);
          const headLeadMs = headPlayAt ? headPlayAt - estimatedServerNowMs() : null;
          const readyForSharedStart =
            headLeadMs == null || headLeadMs >= Math.max(80, STARTUP_MIN_HEAD_LEAD_MS - 100);
          if (queuedMs < minStartupQueueMs || !readyForSharedStart) {
            if (!pausedByUser) setStatus('Buffering', 'buffering');
            return;
          }
          started = true;
          syncPhase = 'converging';
          if (firstPlaybackWallPerf == null) {
            firstPlaybackWallPerf = performance.now();
          }
          nextPlayTime = Math.max(audioCtx.currentTime + 0.035, nextPlayTime);
          setStatus(pausedByUser ? 'Paused' : 'Playing', pausedByUser ? 'paused' : 'playing');
        }
        if (!isPlaybackWarmupWindow()) {
          syncPhase = 'locked';
        }
        const minOffsetSamples = MIN_OFFSET_SAMPLES;
        while (queue.length > 0) {
          const aheadMs = (nextPlayTime - audioCtx.currentTime) * 1000;
          if (aheadMs < -HARD_UNDERRUN_MS) {
            // Hard underrun — the audio chain has fallen so far behind the
            // schedule that resuming smoothly is impossible; rebuffer.
            started = false;
            syncPhase = 'rebuffering';
            if (!pausedByUser) setStatus('Rebuffering', 'rebuffering');
            nextPlayTime = audioCtx.currentTime + 0.08;
            break;
          }
          if (aheadMs < -SOFT_UNDERRUN_MS) {
            // Soft underrun — splice a short silence to keep the chain
            // continuous (no full rebuffer flash).
            const padMs = Math.min(120, -aheadMs - 40);
            if (padMs > 5) {
              enqueueSilence(
                48000,
                1,
                padMs,
                lastReceivedPlayAtMs != null
                  ? lastReceivedPlayAtMs + padMs
                  : null,
              );
            }
            nextPlayTime = audioCtx.currentTime + 0.02;
            continue;
          }
          if (aheadMs > maxBufferMs) break;
          const item = queue.shift();
          queuedMs = Math.max(0, queuedMs - item.durationMs);
          const chainHead = Math.max(nextPlayTime, audioCtx.currentTime + 0.012);
          let scheduleAt = chainHead;
          let playbackRate = 1;
          let correctionMs = 0;
          const hasPlayAt =
            item.playAtServerMs != null &&
            item.playAtServerMs > 0 &&
            clockOffsetSamples.length >= minOffsetSamples;
          if (hasPlayAt) {
            const serverNowMs = estimatedServerNowMs();
            const leadMs = item.playAtServerMs - serverNowMs;
            // Compensate for device-local audio pipeline delay so multiple
            // listeners *audibly* converge even when their output paths
            // (laptop speakers vs Bluetooth vs USB DAC) have very different
            // hardware buffer depths.
            let wallAligned =
              audioCtx.currentTime + leadMs / 1000 - pipelineLatencySec;
            const lateByMs =
              localWallNowMs() - (item.playAtServerMs - clockOffsetMs);
            if (lateByMs > 2200) {
              // Catastrophically stale chunk reference — partially demote
              // the offset samples (keep the recent half) rather than
              // wiping state. A full wipe used to force a long re-warmup;
              // demotion keeps convergence smooth.
              if (clockOffsetSamples.length > 4) {
                clockOffsetSamples = clockOffsetSamples.slice(
                  Math.floor(clockOffsetSamples.length / 2),
                );
              }
              started = false;
              syncPhase = 'warming';
              nextPlayTime = audioCtx.currentTime + 0.08;
              if (!pausedByUser) setStatus('Rebuffering', 'rebuffering');
              break;
            }
            if (wallAligned < audioCtx.currentTime + 0.006) {
              wallAligned = audioCtx.currentTime + 0.006;
            }
            const syncErrorMs = (chainHead - wallAligned) * 1000;
            // Two-sided correction strategy:
            //   - syncErrorMs > 0 ("ahead"): pull schedule slightly back
            //     toward wallAligned via an overlap-style correction; let
            //     playback rate creep above 1.0 so chainHead advances less
            //     per chunk and the chain converges.
            //   - syncErrorMs < 0 ("behind", gap): schedule back-to-back
            //     at chainHead (no audible gap) and let playback rate dip
            //     below 1.0 so chainHead advances *more* per chunk and the
            //     residual closes over several chunks instead of one jolt.
            if (syncErrorMs > 0) {
              correctionMs = overlapCorrectionMs(syncErrorMs, item.durationMs);
              scheduleAt = Math.max(
                wallAligned,
                audioCtx.currentTime + 0.006,
                chainHead - correctionMs / 1000,
              );
            } else {
              // Behind: schedule at chainHead, no gap.
              scheduleAt = Math.max(chainHead, audioCtx.currentTime + 0.006);
              correctionMs = -Math.min(
                item.durationMs * 0.18,
                Math.abs(syncErrorMs) * 0.13,
              );
            }
            const residualMs = (scheduleAt - wallAligned) * 1000;
            playbackRate = rateForResidualBidirectional(residualMs);
            recordSyncProbe(wallAligned, nextPlayTime);
            recordSyncMetrics(
              scheduleAt,
              wallAligned,
              nextPlayTime,
              leadMs,
              queuedMs,
              correctionMs,
              playbackRate,
            );
          }
          const source = audioCtx.createBufferSource();
          source.buffer = item.buffer;
          const chunkGain = audioCtx.createGain();
          const fadeSec = item.isSilence ? 0.002 : 0.005;
          chunkGain.gain.setValueAtTime(0, scheduleAt);
          chunkGain.gain.linearRampToValueAtTime(1, scheduleAt + fadeSec);
          if (source.playbackRate) {
            applyRateRamp(source.playbackRate, scheduleAt, playbackRate);
          }
          source.connect(chunkGain);
          chunkGain.connect(gainNode);
          source.start(scheduleAt);
          nextPlayTime = scheduleAt + item.durationMs / 1000 / playbackRate;
        }
        updateBufferLabel();
      }

      function recordClockOffset(serverMs, clientSentMs, clientNowMs) {
        if (!serverMs || !clientSentMs) return;
        const rtt = Math.max(0, clientNowMs - clientSentMs);
        const offset = serverMs - (clientSentMs + rtt / 2);
        lastRttMs = rtt;
        clockOffsetSamples.push({
          offset,
          rtt,
          at: performance.now(),
        });
        const cap = isJoinWarmupWindow()
          ? 12
          : isPlaybackWarmupWindow()
          ? 18
          : 22;
        if (clockOffsetSamples.length > cap) clockOffsetSamples.shift();
        const byRtt = [...clockOffsetSamples].sort((a, b) => a.rtt - b.rtt);
        const minRtt = byRtt.length ? byRtt[0].rtt : rtt;
        const maxUsableRtt = minRtt + Math.max(18, minRtt * 0.35);
        let usable = byRtt.filter((s) => s.rtt <= maxUsableRtt);
        const minUsable = Math.min(byRtt.length, Math.max(3, Math.ceil(byRtt.length * 0.55)));
        if (usable.length < minUsable) {
          usable = byRtt.slice(0, minUsable);
        }
        const offsets = usable.map((s) => s.offset).sort((a, b) => a - b);
        const mid = Math.floor(offsets.length / 2);
        const nextMedian = offsets.length % 2 === 0
          ? (offsets[mid - 1] + offsets[mid]) / 2
          : offsets[mid];
        offsetSpanMs =
          offsets.length > 1 ? offsets[offsets.length - 1] - offsets[0] : 0;
        if (clockOffsetSamples.length === 1 ||
            isJoinWarmupWindow() ||
            isPlaybackWarmupWindow()) {
          clockOffsetMs = nextMedian;
        } else if (offsetSpanMs < 26) {
          // Tight steady-state median — gentle IIR when locked to avoid harsh
          // long-session offset hunting.
          const locked = syncPhase === 'locked';
          clockOffsetMs = locked
            ? clockOffsetMs * 0.68 + nextMedian * 0.32
            : clockOffsetMs * 0.55 + nextMedian * 0.45;
        } else {
          // Looser jitter — slow IIR to avoid yanking sync on noisy hops.
          clockOffsetMs = clockOffsetMs * 0.78 + nextMedian * 0.22;
        }
        offsetValue.textContent = Math.round(clockOffsetMs) + ' ms';
        modalOffset.textContent = offsetValue.textContent;
        latencyValue.textContent = Math.round(rtt) + ' ms';
        modalRtt.textContent = latencyValue.textContent;
      }

      function sendClientEvent(type, payload) {
        if (!ws || ws.readyState !== WebSocket.OPEN) return;
        if (IS_WAN) {
          ws.send(JSON.stringify({
            type,
            roomId: activeRoom || undefined,
            payload: payload || {}
          }));
        } else {
          ws.send(JSON.stringify({
            type,
            payload: payload || {},
            clientTime: Math.round(localWallNowMs())
          }));
        }
      }

      function startPingLoop() {
        stopPingLoop();
        const run = () => {
          if (!ws || ws.readyState !== WebSocket.OPEN) return;
          const clientTime = Math.round(localWallNowMs());
          if (IS_WAN) {
            ws.send(JSON.stringify({
              type: 'stream.ping',
              roomId: activeRoom || undefined,
              payload: { clientTime }
            }));
          } else {
            ws.send(JSON.stringify({ type: 'stream.ping', clientTime }));
          }
          const joinWarm = isJoinWarmupWindow();
          const warmedPing = clockOffsetSamples.length >= MIN_OFFSET_SAMPLES;
          let delay;
          if (!warmedPing) {
            delay = joinWarm ? 180 : 360;
          } else if (joinWarm || isPlaybackWarmupWindow()) {
            delay = joinWarm ? 300 : 520;
          } else {
            delay = 1200;
          }
          pingTimer = setTimeout(run, delay);
        };
        pingTimer = setTimeout(run, 140);
      }
      function stopPingLoop() {
        if (pingTimer) { clearTimeout(pingTimer); pingTimer = null; }
      }

      function connect() {
        const room = roomInput.value.trim().toUpperCase();
        const pin = pinInput.value.trim();
        if (!room) { setError('Enter a room code.'); return; }
        if (!validateRoomCode(room)) { setError('Room code format is invalid.'); return; }
        if (PIN_REQUIRED && !pin) { setError('This room requires a PIN.'); return; }
        if (pin && !/^[0-9]{6}$/.test(pin)) { setError('PIN must be exactly 6 digits.'); return; }
        setError('');
        activeRoom = room;
        ensureAudio();
        resetPlayback();

        if (ws) { try { ws.close(); } catch (_) {} ws = null; }

        const proto = location.protocol === 'https:' ? 'wss' : 'ws';
        const params = IS_WAN
          ? new URLSearchParams({ peerId: 'web_' + Date.now().toString(36) })
          : new URLSearchParams({ room });
        if (!IS_WAN && pin) params.set('pin', pin);
        const url = proto + '://' + location.host + WS_PATH + '?' + params.toString();

        const socket = new WebSocket(url);
        socket.binaryType = 'arraybuffer';
        ws = socket;
        connectBtn.disabled = true;
        disconnectBtn.disabled = false;
        setStatus('Connecting', 'connecting');
        roomTitle.textContent = room;
        roomSubtitle.textContent = '{{SOURCE_LABEL}}';

        socket.onopen = async () => {
          listenerSessionStartPerf = performance.now();
          if (!IS_WAN) {
            setStatus('Buffering', 'buffering');
            toggleBtn.disabled = false;
            toggleBtn.textContent = '⏸';
            sendClientEvent('listener.ready', { roomId: room });
            startPingLoop();
            closeDetailsIfOpen();
          }
          if (audioCtx.state !== 'running') {
            try { await audioCtx.resume(); } catch (_) {}
          }
          // After resume the platform usually has real outputLatency values.
          refreshPipelineLatency();
        };

        socket.onmessage = (event) => {
          if (event.data instanceof ArrayBuffer) {
            handleBinaryLanFrame(event.data);
            schedulePlayback();
            return;
          }
          if (event.data instanceof Blob) {
            event.data.arrayBuffer().then((buf) => {
              handleBinaryLanFrame(buf);
              schedulePlayback();
            }).catch(() => {});
            return;
          }
          if (typeof event.data !== 'string') return;
          let decoded;
          try { decoded = JSON.parse(event.data); } catch (_) { return; }
          const payload = decoded.payload || decoded;
          switch (decoded.type) {
            case 'connection.ready':
              if (IS_WAN && socket === ws) {
                sendClientEvent('server.hello', {
                  appName: 'SyncWave Browser Listener',
                  appVersion: APP_VERSION,
                  protocolVersion: '{{PROTOCOL}}',
                  clientPlatform: 'web',
                  clientRole: 'listener',
                  listenerOnly: true,
                });
              }
              break;
            case 'server.ready':
              sendClientEvent('room.join', {
                deviceName: 'Browser Listener',
                platform: 'web',
                ...(pin ? { pin } : {})
              });
              break;
            case 'room.joined':
              sendClientEvent('stream.listener_join', {
                roomId: room,
                ...(pin ? { pin } : {}),
              });
              startPingLoop();
              closeDetailsIfOpen();
              break;
            case 'stream.listener_joined':
              if (payload.targetBufferMs) setTargetBuffer(payload.targetBufferMs);
              setStatus('Buffering', 'buffering');
              toggleBtn.disabled = false;
              toggleBtn.textContent = '⏸';
              closeDetailsIfOpen();
              break;
            case 'stream.meta':
              setTargetBuffer(payload.targetBufferMs || DEFAULT_TARGET_BUFFER_MS);
              if (!IS_WAN && Array.isArray(decoded.audioTransports) &&
                  decoded.audioTransports.indexOf('pcm16-binary-v1') !== -1 &&
                  !useBinaryLanTransport) {
                useBinaryLanTransport = true;
                ws.send(JSON.stringify({ type: 'stream.negotiate', transport: 'pcm16-binary-v1' }));
              }
              break;
            case 'stream.audio':
            case 'stream.audio_chunk':
              enqueueChunk(chunkEventFromMessage(decoded));
              schedulePlayback();
              break;
            case 'stream.sync':
              if (payload.targetBufferMs) setTargetBuffer(payload.targetBufferMs);
              break;
            case 'sync.pong':
            case 'stream.pong':
              recordClockOffset(
                Number(payload.serverTime || payload.serverTimestamp || 0),
                Number(payload.clientTime || 0),
                localWallNowMs()
              );
              break;
            case 'stream.listener_count':
              lastListenerCount = Number(payload.count || 0);
              headerListenerWrap.hidden = false;
              headerListenerCount.textContent = String(lastListenerCount);
              break;
            case 'room.join_failed':
            case 'stream.failed':
            case 'server.auth_failed':
            case 'server.auth_required':
            case 'server.unsupported_version':
            case 'stream.host_stopped':
            case 'room.closed':
            case 'error':
              setStatus('Disconnected', 'disconnected');
              setError(payload.message || decoded.message || 'Stream ended.');
              try { socket.close(); } catch (_) {}
              break;
          }
        };

        socket.onerror = () => {
          setStatus('Error', 'error');
          setError('Could not connect to stream.');
          connectBtn.disabled = false;
          disconnectBtn.disabled = true;
          toggleBtn.disabled = true;
        };

        socket.onclose = () => {
          if (ws !== socket) return;
          ws = null;
          stopPingLoop();
          connectBtn.disabled = false;
          disconnectBtn.disabled = true;
          toggleBtn.disabled = true;
          setStatus('Disconnected', 'disconnected');
          started = false;
          headerListenerWrap.hidden = true;
        };
      }

      function disconnect() {
        try { ws && ws.close(); } catch (_) {}
        if (audioCtx) {
          try { audioCtx.suspend(); } catch (_) {}
        }
      }

      async function togglePlayback() {
        if (!audioCtx) return;
        if (!pausedByUser) {
          pausedByUser = true;
          if (gainNode) gainNode.gain.value = 0;
          toggleBtn.textContent = '▶';
          setStatus('Paused', 'paused');
        } else {
          pausedByUser = false;
          if (audioCtx.state !== 'running') {
            await audioCtx.resume();
          }
          if (gainNode) gainNode.gain.value = 1;
          toggleBtn.textContent = '⏸';
          setStatus(started ? 'Playing' : 'Buffering', started ? 'playing' : 'buffering');
        }
      }

      connectBtn.addEventListener('click', connect);
      disconnectBtn.addEventListener('click', disconnect);
      toggleBtn.addEventListener('click', () => { togglePlayback().catch(() => {}); });
      infoBtn.addEventListener('click', () => infoModal.showModal());
      closeInfo.addEventListener('click', () => infoModal.close());
      detailsBtn.addEventListener('click', () => detailsModal.showModal());
      closeDetails.addEventListener('click', () => detailsModal.close());

      function applyDefaultRoomPrefix() {
        if (!roomInput.value.trim() && ROOM_DEFAULT_PREFIX) {
          roomInput.value = ROOM_DEFAULT_PREFIX;
        }
      }

      function maybeAutoOpenDetails() {
        if (!detailsModal) return;
        applyDefaultRoomPrefix();
        const room = roomInput.value.trim().toUpperCase();
        if (!room || room === ROOM_DEFAULT_PREFIX) {
          detailsModal.showModal();
        }
      }
      setTimeout(maybeAutoOpenDetails, 140);
      window.addEventListener('pageshow', function (ev) {
        if (ev.persisted) maybeAutoOpenDetails();
      });

      // Auto-connect when ROOM (and optional PIN) provided via query params.
      const initialRoom = roomInput.value.trim();
      if (initialRoom) {
        // Avoid blocking the gesture-required AudioContext start: schedule
        // connect on next tick so user can still tap Play to unmute autoplay.
        setTimeout(connect, 80);
      }

      window.addEventListener('beforeunload', () => {
        stopPingLoop();
        if (scheduleTimer) clearInterval(scheduleTimer);
        if (latencyRefreshTimer) clearInterval(latencyRefreshTimer);
        if (ws) ws.close();
        if (audioCtx) audioCtx.close();
      });
    })();
