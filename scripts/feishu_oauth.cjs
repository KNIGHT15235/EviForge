/* Manual browser login using the official pinned lark-mcp OAuth implementation.
 * Usage: node scripts/feishu_oauth.cjs <credentials.json> <lark-mcp package dir> <scope list>
 * Secrets never appear in argv, stdout or the browser URL.
 */
'use strict';
const fs = require('node:fs');
const path = require('node:path');
const [credentialFile, packageDir, scopeText] = process.argv.slice(2);
if (!credentialFile || !packageDir || !scopeText) {
  console.error('Specify an ignored credential file, official package directory and explicit OAuth scopes.');
  process.exit(2);
}
const root = path.resolve(packageDir);
const credentials = JSON.parse(fs.readFileSync(credentialFile, 'utf8'));
const privateStore = path.join(path.dirname(path.resolve(credentialFile)), 'feishu-auth');
process.env.LOCALAPPDATA = privateStore;
process.env.APPDATA = privateStore;
const {logger} = require(path.join(root, 'dist/utils/logger.js'));
logger.setLevel(0); // Vendor exception messages may contain sensitive auth data.
const {LarkAuthHandlerLocal} = require(path.join(root, 'dist/auth/handler/handler-local.js'));
const {authStore} = require(path.join(root, 'dist/auth/store.js'));
const {storageManager} = require(path.join(root, 'dist/auth/utils/storage-manager.js'));
const {commonHttpInstance} = require(path.join(root, 'dist/utils/http-instance.js'));
const express = require(path.join(root, '../../express'));
let lastFailure = null;
commonHttpInstance.interceptors.response.use(response => response, error => {
  const body = error.response?.data;
  lastFailure = {
    http_status: error.response?.status || null,
    code: typeof body?.code === 'number' ? body.code : null,
    error: /^[a-z_]{1,64}$/.test(body?.error || '') ? body.error : 'oauth_request_failed',
  };
  console.error(JSON.stringify({state:'failed', stage:'token_exchange', ...lastFailure}));
  return Promise.reject(error);
});
let stage = 'credential_preflight';
(async () => {
  if (!process.argv.includes('--status')) {
    // Do not make the user approve a browser flow when the app credential is invalid.
    const check = await commonHttpInstance.post('https://open.feishu.cn/open-apis/auth/v3/app_access_token/internal', {
      app_id: credentials.EVIFORGE_FEISHU_APP_ID,
      app_secret: credentials.EVIFORGE_FEISHU_APP_SECRET,
    }, {timeout:30000});
    if (check.data.code !== 0 || !check.data.app_access_token) {
      console.error(JSON.stringify({state:'failed', stage, code:check.data.code}));
      process.exit(1);
    }
  }
  stage = 'secure_token_store';
  await authStore.initialize();
  if (!storageManager.isInitializedStorageSuccess) throw new Error('Secure token store unavailable');
  if (process.argv.includes('--status')) {
    const token = await authStore.getLocalAccessToken(credentials.EVIFORGE_FEISHU_APP_ID);
    const stored = token && await authStore.getToken(token);
    console.log(JSON.stringify({state:stored && stored.expiresAt * 1000 > Date.now() ? 'authenticated' : 'authorization_required', scopes:stored?.scopes || []}));
    process.exit(0);
  }
  await authStore.removeLocalAccessToken(credentials.EVIFORGE_FEISHU_APP_ID);
  stage = 'oauth_server';
  const app = express(); app.use(express.json());
  const handler = new LarkAuthHandlerLocal(app, {
    appId: credentials.EVIFORGE_FEISHU_APP_ID,
    appSecret: credentials.EVIFORGE_FEISHU_APP_SECRET,
    domain: 'https://open.feishu.cn', host: 'localhost', port: 3000,
    scope: scopeText.split(/[ ,]+/).filter(Boolean),
  });
  handler.setupRoutes();
  app.use((error, req, res, next) => {
    // Suppress Express' default stack/error rendering, which can expose auth details.
    if (res.headersSent) return next(new Error('OAuth callback failed'));
    res.status(400).type('text/plain').send('Authorization could not be completed. Return to EviForge for a safe error code; do not refresh or reuse this callback.');
  });
  stage = 'authorization_url';
  const {authorizeUrl} = await handler.reAuthorize(undefined, true);
  clearTimeout(handler.timeoutId);
  handler.timeoutId = setTimeout(() => { handler.stopServer(); process.exit(3); }, 1800000);
  console.log(JSON.stringify({state:'authorization_required', url:authorizeUrl}));
  if (process.argv.includes('--open-browser')) {
    // Same official open package/API used by lark-mcp LoginHandler; no UI automation.
    const openModule = require(path.join(root, '../../open'));
    await (openModule.default || openModule)(authorizeUrl);
    console.log(JSON.stringify({state:'system_browser_requested'}));
  }
  const poll = setInterval(async () => {
    const token = await authStore.getLocalAccessToken(credentials.EVIFORGE_FEISHU_APP_ID);
    const stored = token && await authStore.getToken(token);
    if (stored && stored.expiresAt * 1000 > Date.now()) {
      clearInterval(poll);
      console.log(JSON.stringify({state:'authenticated', scopes:stored.scopes}));
      try { await handler.stopServer(); }
      catch (error) {
        // The official callback may have already closed the same local server.
        if (error.code !== 'ERR_SERVER_NOT_RUNNING') throw error;
      }
      process.exit(0);
    }
  }, 2000);
})().catch(error => {console.error(JSON.stringify({state:'failed', stage, error:error.name})); process.exit(1);});
process.on('unhandledRejection', error => {
  console.error(JSON.stringify({state:'failed', stage:'oauth_callback', error:error?.name || 'Error'}));
  process.exit(1);
});
