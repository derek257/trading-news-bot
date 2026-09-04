// Service worker mínimo: solo existe para que el navegador
// permita "instalar" la página como app. No cachea nada,
// así que el dashboard siempre pide los datos más recientes.
self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', () => self.clients.claim());
self.addEventListener('fetch', () => {});
