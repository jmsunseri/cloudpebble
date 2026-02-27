(function() {
    window.PebbleProxySocket = function(proxy, token, authMode) {
        var self = this;
        var mToken = token || '';
        var mAuthMode = authMode || 'v1';
        var mSocket = null;
        var mIsConnected = false;
        var mIsAuthenticated = false;

        _.extend(this, Backbone.Events);

        this.connect = function() {
            if(!proxy) {
                console.log("No proxy server available.");
                _.defer(function() {
                    self.trigger('error', "Websocket proxy not specified.");
                });
                return;
            }
            mSocket = new WebSocket(proxy);
            mSocket.binaryType = "arraybuffer";
            mSocket.onerror = handle_socket_error;
            mSocket.onclose = handle_socket_close;
            mSocket.onmessage = handle_socket_message;
            mSocket.onopen = handle_socket_open;
            console.log("Connecting to " + proxy);
        };

        this.close = function() {
            if(!mSocket) return;
            mSocket.close();
            cleanup();
        };

        this.send = function(data) {
            mSocket.send(data);
        };

        this.isOpen = function() {
            return mIsConnected;
        };

        function cleanup() {
            mSocket = null;
            mIsConnected = false;
            mIsAuthenticated = false;
        }

        function handle_socket_error(e) {
            console.log("socket error", e);
            self.trigger('error', e);
        }

        function handle_socket_open(e) {
            console.log("socket open; authenticating with mode: " + mAuthMode);
            self.trigger('proxy:authenticating');
            try {
                if (mAuthMode === 'v2') {
                    send_v2_auth();
                } else {
                    send_v1_auth();
                }
            } catch (err) {
                self.trigger('error', err.message || err);
            }
        }

        function utf8_bytes(str) {
            if (typeof TextEncoder !== 'undefined') {
                return new TextEncoder().encode(str);
            }
            var encoded = unescape(encodeURIComponent(str));
            return new Uint8Array(_.invoke(encoded, 'charCodeAt', 0));
        }

        function send_v1_auth() {
            if (mToken.length > 255) {
                throw new Error("Proxy v1 auth token exceeds 255 bytes.");
            }
            self.send(new Uint8Array([0x09, mToken.length].concat(_.invoke(mToken, 'charCodeAt', 0))));
        }

        function send_v2_auth() {
            var tokenBytes = utf8_bytes(mToken);
            if (tokenBytes.length > 65535) {
                throw new Error("Proxy v2 auth token exceeds 65535 bytes.");
            }
            var frame = new Uint8Array(3 + tokenBytes.length);
            // Match CoreApp/pebble-tool v2 auth frame format.
            frame[0] = 0x19;
            frame[1] = (tokenBytes.length >> 8) & 0xFF;
            frame[2] = tokenBytes.length & 0xFF;
            frame.set(tokenBytes, 3);
            self.send(frame);
        }

        function handle_socket_message(e) {
            var data = new Uint8Array(e.data);
            if(data[0] == 0x09) {
                if(data[1] == 0x00) {
                    self.trigger('proxy:waiting');
                    console.log("Authenticated successfully.");
                    mIsAuthenticated = true;
                } else {
                    console.log("Authentication failed.");
                    self.trigger('error', "Proxy rejected authentication token.");
                }
            } else if(data[0] == 0x08) {
                if(data[1] == 0xFF) {
                    console.log("Connected successfully.");
                    mIsConnected = true;
                    self.trigger('open');
                } else if(data[1] == 0x00) {
                    console.log("Connection closed remotely.");
                    self.trigger('close', {wasClean: true});
                }
            } else {
                self.trigger('message', data);
            }
        }

        function handle_socket_close(e) {
            console.log("Socket closed.");
            self.trigger('close', e);
            cleanup();
        }
    }
})();
