$(function() {
    jquery_csrf_setup();

    firebase.initializeApp({
        apiKey: "AIzaSyBZ9Cdvwwv9At2lPmc8TxyyEqSXGXejGvc",
        authDomain: "coreapp-ce061.firebaseapp.com",
        projectId: "coreapp-ce061",
        storageBucket: "coreapp-ce061.firebasestorage.app",
        messagingSenderId: "460977838956",
        appId: "1:460977838956:web:9a11a68ec78008fe303149",
        measurementId: "G-J99JXFWEZL"
    });

    var gMainContent = $('.main-container');

    // If we just signed out, clear Firebase client-side auth too
    var justSignedOut = location.search.indexOf('signed_out=1') !== -1;
    if (justSignedOut) {
        firebase.auth().signOut();
    }

    // SSO: if cross-domain session cookie exists, auto-sign-in via custom token
    if (!justSignedOut) {
        $.get('/accounts/api/sso-custom-token').done(function(data) {
            if (data.customToken) {
                firebase.auth().signInWithCustomToken(data.customToken).then(function(result) {
                    return result.user.getIdToken();
                }).then(function(idToken) {
                    return Ajax.Post('/accounts/api/firebase-login', {id_token: idToken});
                }).then(function() {
                    location.href = '/ide/';
                });
            }
        });
        // .done() silently ignores failures (401 = no session = normal)
    }

    // Show "Last Used" badge on last-used provider
    var lastProviderMatch = document.cookie.match(/(?:^|; )last_auth_provider=(\w+)/);
    if (lastProviderMatch) {
        var btn = $('.provider-btn[data-provider="' + lastProviderMatch[1] + '"]');
        btn.append(' <span style="font-size:0.7em; opacity:0.6;">(last used)</span>');
    }

    $('.btn-show-login').click(function() {
        gMainContent.addClass('show-login');
    });

    $('.btn-hide-login').click(function() {
        gMainContent.removeClass('show-login');
    });

    if(location.hash == '#login') {
        gMainContent.addClass('show-login');
    }

    $('.btn-firebase-auth').click(function() {
        $('#provider-chooser').modal('show');
    });

    $('.provider-btn').click(function() {
        var providerName = $(this).data('provider');
        var provider;
        if (providerName === 'google') {
            provider = new firebase.auth.GoogleAuthProvider();
        } else if (providerName === 'github') {
            provider = new firebase.auth.GithubAuthProvider();
        } else if (providerName === 'apple') {
            provider = new firebase.auth.OAuthProvider('apple.com');
        }

        $('#provider-chooser').find('.btn').attr('disabled', 'disabled');

        firebase.auth().signInWithPopup(provider).then(function(result) {
            // Remember which provider was used (survives sign-out)
            var domainAttr = location.hostname.endsWith('.repebble.com') ? '; Domain=.repebble.com' : '';
            var secureAttr = location.protocol === 'https:' ? '; Secure' : '';
            document.cookie = 'last_auth_provider=' + providerName + '; Max-Age=31536000; Path=/; SameSite=Lax' + domainAttr + secureAttr;
            return result.user.getIdToken();
        }).then(function(idToken) {
            // Create Django session (also sets cross-domain cookie server-side)
            return Ajax.Post('/accounts/api/firebase-login', {
                id_token: idToken
            });
        }).then(function() {
            location.href = '/ide/';
        })['catch'](function(error) {
            if (error && error.code === 'auth/account-exists-with-different-credential') {
                var pendingCred = error.credential;
                var email = error.customData ? error.customData.email : '';
                var otherProviders = ['google', 'github', 'apple'].filter(function(p) { return p !== providerName; });
                var providerNames = {google: 'Google', github: 'GitHub', apple: 'Apple'};
                var otherLabels = otherProviders.map(function(p) { return providerNames[p]; }).join(' or ');
                alert('An account already exists with ' + (email || 'this email') + ' using a different sign-in method. Please sign in with ' + otherLabels + ' to link your accounts.');
                var tryProvider;
                var tryName = otherProviders[0];
                if (tryName === 'google') {
                    tryProvider = new firebase.auth.GoogleAuthProvider();
                } else if (tryName === 'github') {
                    tryProvider = new firebase.auth.GithubAuthProvider();
                } else if (tryName === 'apple') {
                    tryProvider = new firebase.auth.OAuthProvider('apple.com');
                }
                if (email) tryProvider.setCustomParameters({login_hint: email});
                return firebase.auth().signInWithPopup(tryProvider).then(function(result) {
                    return result.user.linkWithCredential(pendingCred);
                }).then(function(result) {
                    return result.user.getIdToken();
                }).then(function(idToken) {
                    return Ajax.Post('/accounts/api/firebase-login', {
                        id_token: idToken
                    });
                }).then(function() {
                    location.href = '/ide/';
                });
            } else if (!error || error.code !== 'auth/popup-closed-by-user') {
                alert(error.message || error);
            }
        })['finally'](function() {
            $('#provider-chooser').find('.btn').removeAttr('disabled');
        });
    });
});
