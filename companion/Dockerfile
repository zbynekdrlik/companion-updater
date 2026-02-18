# Using :latest tag for easier updates via companion-updater dashboard
FROM ghcr.io/bitfocus/companion/companion:latest

USER root

ENV TZ=Europe/Bratislava

RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        libnss-mdns \
        dbus \
        avahi-daemon \
        avahi-utils \
        tzdata \
        curl \
        gnupg \
    && ln -fs /usr/share/zoneinfo/Europe/Bratislava /etc/localtime \
    && dpkg-reconfigure -f noninteractive tzdata \
    && rm -rf /var/lib/apt/lists/*

RUN mkdir -p --mode=0755 /usr/share/keyrings \
    && curl -fsSL https://pkg.cloudflare.com/cloudflare-main.gpg -o /usr/share/keyrings/cloudflare-main.gpg \
    && echo 'deb [signed-by=/usr/share/keyrings/cloudflare-main.gpg] https://pkg.cloudflare.com/cloudflared any main' > /etc/apt/sources.list.d/cloudflared.list \
    && apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y cloudflared \
    && rm -rf /var/lib/apt/lists/*

RUN sed -i 's/^hosts:.*/hosts:          files mdns4_minimal [NOTFOUND=return] mdns4 mdns dns/' /etc/nsswitch.conf

COPY entrypoint.sh /usr/local/bin/companion-entrypoint.sh
RUN chmod +x /usr/local/bin/companion-entrypoint.sh

ENTRYPOINT ["/usr/local/bin/companion-entrypoint.sh"]
