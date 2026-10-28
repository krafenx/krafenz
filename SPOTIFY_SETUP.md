# Spotify playback widget setup

The widget reads playback state through a server-side Spotify OAuth connection. Spotify access and refresh tokens stay in the Cloudflare Worker and its existing `WATCHLIST` KV namespace; the browser only receives track metadata and progress.

Spotify currently requires the app owner to have an active Premium subscription for a Development Mode Web API app. Without it, the widget automatically falls back to Last.fm for track details and hides the timeline rather than showing invented progress.

## Connect the Spotify app

1. Create an app in the [Spotify Developer Dashboard](https://developer.spotify.com/dashboard).
2. Add this exact Redirect URI to the app settings:

   ```text
   https://krafen.me/api/spotify/callback
   ```

3. Save the app's Client ID and Client Secret in the Cloudflare Secret Store used by this Worker. The `SPOTIFY_CLIENT_ID` and `SPOTIFY_CLIENT_SECRET` bindings are declared in `wrangler.jsonc`; both matching secrets must exist in that store before deployment.

   - `SPOTIFY_CLIENT_ID`
   - `SPOTIFY_CLIENT_SECRET`

4. Deploy the Worker and static assets.
5. Open `https://krafen.me/watchlist` or `https://krafen.me/setup`, enter admin mode, then click **подключить Spotify** in the admin bar. This starts OAuth from the authenticated site session.
6. Approve access with the Spotify account whose playback the widget should show. Spotify returns to the site and the Worker saves its refresh token in `WATCHLIST`.

The widget updates playback about every five seconds and animates progress once per second from Spotify's reported position. When playback is paused, progress freezes. When nothing is playing, the widget shows the most recently played track without a timeline.

The authorization asks for `user-read-playback-state` and `user-read-recently-played`. Spotify refresh tokens can expire after six months, so reconnect from the same admin session if Spotify authorization expires.

For another hostname or preview deployment, register that deployment's exact HTTPS callback URL in the Spotify app and start authorization from that same hostname.
