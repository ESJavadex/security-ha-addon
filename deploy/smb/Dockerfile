FROM alpine:3.22
RUN apk add --no-cache samba-server samba-common-tools tini \
 && addgroup -g 1000 vault && adduser -D -H -s /sbin/nologin -u 1000 -G vault vault \
 && adduser -D -H -s /sbin/nologin -u 1001 seccam \
 && adduser -D -H -s /sbin/nologin -u 1002 hass
COPY smb.conf /etc/samba/smb.conf
COPY entrypoint.sh /entrypoint.sh
RUN chmod 755 /entrypoint.sh
ENTRYPOINT ["/sbin/tini", "--", "/entrypoint.sh"]
