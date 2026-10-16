<script setup>
import { computed, onMounted, onUnmounted, ref } from 'vue'

const loggedIn = ref(false), loading = ref(true), username = ref('lj'), password = ref('')
const error = ref(''), notice = ref(''), busy = ref(false), uploading = ref(false)
const system = ref(null), jobs = ref([]), selectedId = ref(null), logs = ref(''), showLogs = ref(false)
const imageUrl = ref('/api/uploads/sample.png'), imageInput = ref(null), tick = ref(Date.now())
const prompt = ref('A man in a quiet office looks at the camera and smiles gently. Natural motion, soft indoor lighting. He says, "Welcome to MOVA video studio."')
const model = ref('MOVA-720p'), frames = ref(49), steps = ref(25), seed = ref(67)
const negative = ref(''), imageId = ref('sample.png')
let timer
const selected = computed(() => jobs.value.find(j => j.id === selectedId.value) || jobs.value[0])
const pending = computed(() => jobs.value.filter(j => ['running', 'queued'].includes(j.status)).length)
const available = computed(() => system.value?.worker?.online)
const labels = { queued: '排队中', running: '生成中', succeeded: '已完成', failed: '失败', cancelled: '已取消' }
const active = job => ['running', 'queued'].includes(job?.status)
const formatTime = stamp => new Date(stamp * 1000).toLocaleString('zh-CN', {month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'})
function elapsed(job) {
  const seconds = Math.max(0, Math.floor(((job.finished || tick.value / 1000) - (job.started || job.created))))
  return seconds < 60 ? `${seconds} 秒` : `${Math.floor(seconds / 60)} 分 ${seconds % 60} 秒`
}
async function api(path, options = {}) {
  const response = await fetch('/api' + path, { ...options, headers: { ...(options.body instanceof FormData ? {} : {'Content-Type':'application/json'}), ...options.headers } })
  if (response.status === 401 && path !== '/login') loggedIn.value = false
  if (!response.ok) {
    const data = await response.json().catch(() => ({}))
    throw new Error(typeof data.detail === 'string' ? data.detail : (response.status === 422 ? '生成参数不正确，请检查后重试' : `请求失败 (${response.status})`))
  }
  return response.json()
}
async function refresh() {
  if (!loggedIn.value) return
  try {
    const [info, items] = await Promise.all([api('/system'), api('/jobs')])
    system.value = info; jobs.value = items
    if (showLogs.value && selected.value) logs.value = (await api(`/jobs/${selected.value.id}/logs`)).text
  } catch (e) { error.value = e.message }
}
async function login() {
  busy.value = true; error.value = ''
  try { await api('/login', {method:'POST', body:JSON.stringify({username:username.value,password:password.value})}); password.value = ''; loggedIn.value = true; await refresh() }
  catch(e) { error.value = e.message }
  finally { busy.value = false }
}
async function logout() { await api('/logout', {method:'POST'}); loggedIn.value = false; jobs.value = []; system.value = null }
async function upload(event) {
  const file = event.target.files?.[0]
  if (!file) return
  uploading.value = true; error.value = ''
  try {
    const data = new FormData(); data.append('file', file)
    const result = await api('/uploads', {method:'POST', body:data})
    imageId.value = result.image_id; imageUrl.value = result.url
  } catch(e) { error.value = e.message }
  finally { uploading.value = false; event.target.value = '' }
}
async function generate() {
  busy.value = true; error.value = ''; notice.value = ''
  try {
    const job = await api('/jobs', {method:'POST', body:JSON.stringify({prompt:prompt.value,negative_prompt:negative.value,image_id:imageId.value,model:model.value,num_frames:Number(frames.value),steps:Number(steps.value),seed:Number(seed.value)})})
    selectedId.value = job.id; showLogs.value = false; await refresh()
  } catch(e) { error.value = e.message }
  finally { busy.value = false }
}
async function cancel(job) {
  error.value = ''
  try { await api(`/jobs/${job.id}/cancel`, {method:'POST'}); await refresh() }
  catch(e) { error.value = e.message }
}
function reuse(job) {
  const p = job.params
  prompt.value = p.prompt; negative.value = p.negative_prompt; model.value = p.model
  frames.value = p.num_frames; steps.value = p.steps; seed.value = p.seed; imageId.value = p.image_id
  imageUrl.value = `/api/uploads/${p.image_id}`; notice.value = '已载入该任务的参数，可修改后再次生成'
  window.scrollTo({top:0, behavior:'smooth'})
}
function select(job) { selectedId.value = job.id; showLogs.value = false; logs.value = '' }
async function toggleLogs() {
  showLogs.value = !showLogs.value
  if (showLogs.value && selected.value) {
    try { logs.value = (await api(`/jobs/${selected.value.id}/logs`)).text }
    catch(e) { error.value = e.message }
  }
}
onMounted(async () => {
  try { await api('/session'); loggedIn.value = true; await refresh() } catch {}
  loading.value = false
  timer = setInterval(() => { tick.value = Date.now(); refresh() }, 5000)
})
onUnmounted(() => clearInterval(timer))
</script>

<template>
  <div v-if="loading" class="boot">MOVA <span>正在连接工作台…</span></div>
  <main v-else-if="!loggedIn" class="login-page">
    <div class="login-art"><div class="wordmark">mova<span> / studio</span></div><p class="eyebrow">IDEAS INTO MOTION</p><h1>让想象<br>有声有色。</h1><p>从一张图片开始，生成属于你的音视频。</p><div class="art-lines" aria-hidden="true"><i></i><i></i><i></i><i></i><i></i></div><small>POWERED BY SGLANG × MOVA</small></div>
    <form class="login-form" @submit.prevent="login"><span class="eyebrow">YOUR CREATIVE WORKSPACE</span><h2>登录工作台</h2><p class="muted">连接你的 GPU，开始创作。</p><label for="username">用户名</label><input id="username" v-model="username" autocomplete="username" required><label for="password">密码</label><input id="password" v-model="password" type="password" autocomplete="current-password" required><p v-if="error" class="error" role="alert">{{error}}</p><button class="primary" :disabled="busy">{{busy ? '正在登录…' : '进入工作台 ↗'}}</button><small>独立部署 · 任务与素材保存在本服务器</small></form>
  </main>
  <div v-else class="app-shell">
    <header class="topbar"><a class="wordmark" href="/">mova<span> / studio</span></a><div class="top-right"><span class="connection" :class="{online:available}"><i></i>{{available ? 'GPU 工作进程在线' : 'GPU 工作进程离线'}}</span><span class="user">{{username}}</span><button class="text-button" @click="logout">退出</button></div></header>
    <main class="workspace">
      <section class="page-title"><div><p class="eyebrow">VIDEO GENERATION WORKSPACE</p><h1>把灵感，变成下一帧。</h1><p class="muted">图片、文字、声音，在这里一起发生。</p></div><span class="engine-badge">SGLANG <b>×</b> MOVA</span></section>
      <div v-if="error" class="banner error" role="alert">{{error}}<button class="text-button" @click="error=''">关闭</button></div>
      <div v-if="notice" class="banner notice" role="status">{{notice}}<button class="text-button" @click="notice=''">关闭</button></div>
      <div class="studio-grid">
        <form class="panel controls" @submit.prevent="generate">
          <div class="panel-heading"><h2>创作设置</h2><span class="tiny-label">01 / CREATE</span></div>
          <label class="section-label" for="source-image">参考图片 <span>作为视频的起始画面</span></label>
          <input id="source-image" ref="imageInput" type="file" accept="image/png,image/jpeg,image/webp" class="sr-only" @change="upload">
          <button class="upload-zone" type="button" @click="imageInput.click()" :disabled="uploading"><img :src="imageUrl" alt="当前参考图片"><div><strong>{{uploading ? '上传中…' : '更换参考图片 ↗'}}</strong><span>PNG / JPG / WebP · 最大 20 MB</span></div></button>
          <label class="section-label" for="prompt">画面与声音描述 <span>{{prompt.length}} / 4000</span></label><textarea id="prompt" v-model="prompt" rows="5" minlength="5" maxlength="4000" required placeholder="描述场景、人物动作和想要听到的对白或声音…"></textarea><p class="field-hint">描述人物动作、镜头与对白；英文描述通常更稳定。</p>
          <div class="two-col"><div><label for="model">模型 / 清晰度</label><select id="model" v-model="model"><option value="MOVA-720p">MOVA · 720p</option><option value="MOVA-360p">MOVA · 360p</option></select></div><div><label for="frames">时长</label><select id="frames" v-model="frames"><option :value="49">约 2 秒 · 49 帧</option><option :value="97">约 4 秒 · 97 帧</option><option :value="193">约 8 秒 · 193 帧</option></select></div></div>
          <details class="advanced"><summary>高级参数 <span>采样步数 / 随机种子</span></summary><div class="two-col"><div><label for="steps">采样步数</label><input id="steps" type="number" min="4" max="50" v-model="steps" required></div><div><label for="seed">随机种子</label><input id="seed" type="number" min="0" max="2147483647" v-model="seed" required></div></div><p class="field-hint">4 步适合流程测试；正式生成建议 25–50 步。</p><label for="negative">负面提示词（可选）</label><textarea id="negative" v-model="negative" rows="2" maxlength="2000" placeholder="不希望出现的内容，留空使用模型默认值"></textarea></details>
          <button class="primary generate-button" :disabled="busy || uploading || !available">{{busy ? '提交中…' : '✦ 生成视频'}}<span>↗</span></button><p class="field-hint centered">{{pending ? `${pending} 个任务处理中 · 串行生成` : '24 FPS · 同步音视频 · 双 GPU 推理'}}</p>
        </form>
        <section class="panel preview-panel"><div class="panel-heading"><h2>预览</h2><span class="tiny-label">02 / PREVIEW</span></div>
          <div class="preview-screen" :class="{ready:selected?.status==='succeeded'}">
            <video v-if="selected?.status==='succeeded'" :key="selected.id" :src="selected.video_url" controls playsinline preload="metadata"></video>
            <div v-else-if="selected" class="generation-state"><div class="motion-icon" :class="{spinning:active(selected)}">✦</div><span class="status-pill" :class="selected.status">{{labels[selected.status]}}</span><h3>{{selected.stage}}</h3><p v-if="active(selected)">{{selected.status==='queued' ? '前序任务完成后将自动开始' : '模型加载与采样需要一些时间，可在下方查看日志'}}</p><p v-else>可查看日志定位问题，或载入参数再次生成。</p><div v-if="active(selected)" class="progress-track"><i :style="{width:selected.progress+'%'}"></i></div><span v-if="active(selected)" class="progress-caption">{{selected.progress}}% · {{elapsed(selected)}}</span></div>
            <div v-else class="empty-preview"><div class="empty-mark">mova</div><h3>每个故事，都从一帧开始。</h3><p>设置图片与描述，点击「生成视频」<br>你的作品将在这里出现。</p><span>IMAGE → MOTION + SOUND</span></div>
          </div>
          <div class="preview-meta" v-if="selected"><div><strong>{{selected.params.model}} <span>· {{selected.params.num_frames}} 帧 · 24 FPS</span></strong><p>{{formatTime(selected.created)}} <span v-if="selected.started">· 耗时 {{elapsed(selected)}}</span></p></div><a v-if="selected.status==='succeeded'" class="secondary" :href="selected.video_url+'?download=true'">下载 MP4 ↓</a><button v-else-if="active(selected)" class="secondary" :disabled="!!selected.cancel_requested" @click="cancel(selected)">{{selected.cancel_requested ? '正在取消…' : '取消任务'}}</button></div>
          <div class="preview-footer" v-else><span>音画同步生成</span><span>本地模型 · 本地存储</span></div>
          <div v-if="selected" class="job-detail"><p class="prompt-display">{{selected.params.prompt}}</p><p v-if="selected.error" class="error job-error">{{selected.error}}</p><div class="detail-actions"><button class="text-button" @click="reuse(selected)">载入参数 ↗</button><button class="text-button" @click="toggleLogs">{{showLogs ? '收起日志 −' : '查看生成日志 +'}}</button></div><pre v-if="showLogs" class="logs">{{logs || '任务还没有日志。'}}</pre></div>
        </section>
      </div>
      <section class="history"><div class="history-heading"><h2>创作记录 <span>{{jobs.length.toString().padStart(2,'0')}}</span></h2><button class="text-button" @click="refresh">刷新 ↻</button></div><div v-if="!jobs.length" class="empty-history">还没有作品。开始你的第一次生成。</div><div v-else class="history-grid"><button v-for="job in jobs" :key="job.id" class="history-card" :class="{selected:selected?.id===job.id}" @click="select(job)"><div class="history-thumb"><img :src="`/api/uploads/${job.params.image_id}`" alt="任务参考图片" loading="lazy"><span class="status-pill" :class="job.status">{{labels[job.status]}}</span><span v-if="job.status==='succeeded'" class="play-symbol">▶</span><span class="duration">{{job.params.num_frames===193?'8':job.params.num_frames===97?'4':'2'}}s</span></div><p>{{job.params.prompt}}</p><div class="history-info"><span>{{job.params.model.replace('MOVA-','')}} · {{job.params.steps}} 步</span><span>{{formatTime(job.created)}}</span></div></button></div></section>
      <footer class="bottom-bar"><span>MOVA STUDIO / 让创作持续发生</span><span>SGLang × MOVA · GPU {{system?.worker?.gpus || '—'}}</span></footer>
    </main>
  </div>
</template>
