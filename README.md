# Mikrotik-routeros
**Install and run mikrotik routeros using docker**

If you want to install a Mikrotik on the server, but also use the rest of the server, you should use Docker.

## Pull the image
```
docker pull ghcr.io/im-ecorp/mikrotik-routeros:latest
```
or
```
docker pull hossein3piol/mikrotik-routeros:latest
```
You can use version tag instead of `latest`

for example `hossein3piol/mikrotik-routeros:7.9`

## Run container using command

* command line

  ```
  docker run -itd \
    --name mikrotik \
    -p 80:80 -p 443:443 -p 1194:1194 -p 8291:8291 -p 8729:8729 \
    --cap-add=NET_ADMIN \
    --device=/dev/net/tun \
    ghcr.io/im-ecorp/mikrotik-routeros:latest
  ```

to stop the container

```
docker stop mikrotik
docker rm mikrotik
```

## Run container using compose

* compose file

  ```
  version: '3.9'

  services:
     routers:
         container_name: "mikrotik"
         image: ghcr.io/im-ecorp/mikrotik-routeros:latest
         privileged: true
         ports:
             - "21:21"    #ftp
             - "22:22"    #ssh
             - "23:23"    #telnet
             - "80:80"    #www
             - "443:443"  #www-ssl
             - "1194:1194"  #OVPN
             - "1450:1450"  #L2TP
             - "8291:8291"  #winbox
             - "8728:8728"  #api
             - "8729:8729"  #api-ssl
             - "13231:13231"  #WireGuard
         cap_add:
             - NET_ADMIN
         devices:
             - /dev/net/tun
  ```
