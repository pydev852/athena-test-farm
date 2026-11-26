/*
 * phone-S20 flap test client (device side, shell uid 2000 -> goes through the VPN; static aarch64).
 * Build on the aarch64 host: gcc -O2 -static -o mqflap mqflap.c
 *
 * mq <ip> <port> <topic> <tag> <ka_s> <dur_s>
 *   Long-lived MQTT 3.1.1 subscription (stand-in for a push socket). CONNECT (server keepalive 2*ka_s), SUBSCRIBE
 *   QoS0 to <topic>, logs every PUBLISH "P<seq> <host_epoch>" (host publishes every 5 s).
 *   App keepalive: PINGREQ every <ka_s> s, PINGRESP expected within 10 s. No PINGRESP, FIN, RST/error ->
 *   close and reconnect (1 s back-off, 5 s connect timeout, 10 s CONNACK/SUBACK timeout).
 *
 * probe <ip> <port> <hosthdr> <tag> <interval_ms> <dur_s>
 *   Short request every <interval_ms>: fresh TCP connect (3 s timeout) + "HEAD / HTTP/1.0" + first response line
 *   (3 s timeout). Logs "PROBE <n> OK|FAIL <stage> <err> dt=<s>". Runs each probe in a forked child so a hanging
 *   probe does not delay the schedule.
 */
#define _GNU_SOURCE
#include <arpa/inet.h>
#include <errno.h>
#include <fcntl.h>
#include <netinet/in.h>
#include <poll.h>
#include <signal.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/time.h>
#include <sys/wait.h>
#include <unistd.h>

static double now(void) { struct timeval tv; gettimeofday(&tv, NULL); return tv.tv_sec + tv.tv_usec / 1e6; }
static void lg(const char *fmt, ...) {
	va_list ap; char b[1024]; int n = snprintf(b, sizeof b, "%.3f ", now());
	va_start(ap, fmt); n += vsnprintf(b + n, sizeof b - n, fmt, ap); va_end(ap);
	if (n > (int)sizeof b - 2) n = sizeof b - 2;
	b[n++] = '\n'; write(1, b, n);
}
static int tconnect(const char *h, int p, int ms, int *err, int *lport) {
	struct sockaddr_in a; memset(&a, 0, sizeof a);
	a.sin_family = AF_INET; a.sin_port = htons(p); inet_pton(AF_INET, h, &a.sin_addr);
	int fd = socket(AF_INET, SOCK_STREAM, 0);
	if (fd < 0) { *err = errno; return -1; }
	fcntl(fd, F_SETFL, O_NONBLOCK);
	int r = connect(fd, (struct sockaddr *)&a, sizeof a);
	if (r < 0 && errno != EINPROGRESS) { *err = errno; close(fd); return -1; }
	if (r < 0) {
		struct pollfd pf = {fd, POLLOUT, 0};
		r = poll(&pf, 1, ms);
		if (r <= 0) { *err = ETIMEDOUT; close(fd); return -1; }
		int e = 0; socklen_t l = sizeof e; getsockopt(fd, SOL_SOCKET, SO_ERROR, &e, &l);
		if (e) { *err = e; close(fd); return -1; }
	}
	struct sockaddr_in la; socklen_t ll = sizeof la; getsockname(fd, (struct sockaddr *)&la, &ll);
	if (lport) *lport = ntohs(la.sin_port);
	*err = 0; return fd;
}
/* ---------------- MQTT ---------------- */
static int rlen(unsigned char *o, int n) { int i = 0; do { int b = n % 128; n /= 128; o[i++] = b | (n ? 128 : 0); } while (n); return i; }
static int s16(unsigned char *o, const char *s) { int l = strlen(s); o[0] = l >> 8; o[1] = l & 255; memcpy(o + 2, s, l); return l + 2; }
static int sendall(int fd, const unsigned char *b, int n) { return send(fd, b, n, MSG_NOSIGNAL) == n ? 0 : -1; }
static int mq_connect_pkt(unsigned char *o, const char *cid, int ka) {
	unsigned char v[300]; int n = s16(v, "MQTT"); v[n++] = 4; v[n++] = 2; v[n++] = ka >> 8; v[n++] = ka & 255; n += s16(v + n, cid);
	int m = 0; o[m++] = 0x10; m += rlen(o + m, n); memcpy(o + m, v, n); return m + n;
}
static int mq_sub_pkt(unsigned char *o, const char *topic) {
	unsigned char v[300]; int n = 0; v[n++] = 0; v[n++] = 1; n += s16(v + n, topic); v[n++] = 0;
	int m = 0; o[m++] = 0x82; m += rlen(o + m, n); memcpy(o + m, v, n); return m + n;
}
static int mq_main(char **argv) {
	const char *H = argv[2]; int P = atoi(argv[3]); const char *TOP = argv[4], *TAG = argv[5];
	double ka = atof(argv[6]), end = now() + atof(argv[7]);
	int fd = -1, nconn = 0, ready = 0, pingwait = 0, npings = 0; double last_close = 0, t_conn = 0, ping_next = 0, ping_sent = 0, t_hs = 0;
	static unsigned char rb[65536]; int rl = 0;
	lg("START mq host=%s port=%d topic=%s tag=%s ka=%.0f", H, P, TOP, TAG, ka);
#define MCLOSE(why) do { lg("CLOSE conn=%d why=%s age=%.1f ready=%d", nconn, why, now() - t_conn, ready); close(fd); fd = -1; rl = 0; ready = 0; pingwait = 0; last_close = now(); } while (0)
	while (now() < end && access("stop", F_OK) != 0) {
		if (fd < 0) {
			if (now() - last_close < 1) { usleep(100000); continue; }
			int err, lp = 0; double t = now(); nconn++;
			fd = tconnect(H, P, 5000, &err, &lp);
			if (fd < 0) { lg("CONN conn=%d FAIL err=%s dt=%.2f", nconn, strerror(err), now() - t); last_close = now(); continue; }
			unsigned char b[600]; char cid[64]; snprintf(cid, sizeof cid, "s20f-%s-%d-%d", TAG, getpid(), nconn);
			int n = mq_connect_pkt(b, cid, (int)(ka * 2));
			n += mq_sub_pkt(b + n, TOP);
			if (sendall(fd, b, n)) { lg("CONN conn=%d write-fail err=%s", nconn, strerror(errno)); close(fd); fd = -1; last_close = now(); continue; }
			t_conn = t_hs = now();
			lg("CONN conn=%d TCPOK lport=%d dt=%.2f", nconn, lp, now() - t);
		}
		if (!ready && now() - t_hs > 10) { MCLOSE("handshake-timeout-10s"); continue; }
		if (ready && !pingwait && now() >= ping_next) {
			unsigned char pr[2] = {0xc0, 0};
			npings++;
			if (sendall(fd, pr, 2)) { char w[80]; snprintf(w, sizeof w, "ping-write-err:%s", strerror(errno)); MCLOSE(w); continue; }
			pingwait = npings; ping_sent = now();
		}
		if (pingwait && now() - ping_sent > 10) { lg("PING %d conn=%d NORESP", pingwait, nconn); MCLOSE("ping-noresp-10s"); continue; }
		struct pollfd pf = {fd, POLLIN, 0};
		if (poll(&pf, 1, 100) <= 0) continue;
		int n = recv(fd, rb + rl, sizeof rb - rl, 0);
		if (n == 0) { MCLOSE("peer-FIN"); continue; }
		if (n < 0) { char w[80]; snprintf(w, sizeof w, "recv-err:%s", strerror(errno)); MCLOSE(w); continue; }
		rl += n;
		for (;;) { /* parse packets */
			if (rl < 2) break;
			int len = 0, mul = 1, i = 1, ok = 0;
			while (i < rl && i < 5) { len += (rb[i] & 127) * mul; mul *= 128; if (!(rb[i] & 128)) { ok = 1; i++; break; } i++; }
			if (!ok || rl < i + len) break;
			int t = rb[0] & 0xf0; unsigned char *d = rb + i;
			if (t == 0x20) { lg("CONNACK conn=%d rc=%d hs=%.2f", nconn, len >= 2 ? d[1] : -1, now() - t_hs); }
			else if (t == 0x90) { ready = 1; ping_next = now() + ka; lg("READY conn=%d hs=%.2f", nconn, now() - t_hs); }
			else if (t == 0xd0) { lg("PINGRESP %d conn=%d rtt=%.3f", pingwait, nconn, now() - ping_sent); pingwait = 0; ping_next = now() + ka; }
			else if (t == 0x30) {
				int tl = (d[0] << 8) | d[1]; char pl[200]; int pn = len - 2 - tl; if (pn > 199) pn = 199; if (pn < 0) pn = 0;
				memcpy(pl, d + 2 + tl, pn); pl[pn] = 0;
				int seq = 0; double ht = 0; sscanf(pl, "P%d %lf", &seq, &ht);
				lg("RXPUSH %d conn=%d delay=%.3f", seq, nconn, now() - ht);
			} else lg("RX conn=%d type=0x%02x len=%d", nconn, rb[0], len);
			memmove(rb, rb + i + len, rl - i - len); rl -= i + len;
		}
	}
	lg("END mq conns=%d", nconn);
	return 0;
}
/* ---------------- probe ---------------- */
static void probe1(const char *H, int P, const char *hh, int k) {
	double t = now(); int err, lp;
	int fd = tconnect(H, P, 3000, &err, &lp);
	if (fd < 0) { lg("PROBE %d FAIL connect %s dt=%.2f", k, strerror(err), now() - t); return; }
	char b[256]; int n = snprintf(b, sizeof b, "HEAD / HTTP/1.0\r\nHost: %s\r\n\r\n", hh);
	if (send(fd, b, n, MSG_NOSIGNAL) != n) { lg("PROBE %d FAIL write %s dt=%.2f", k, strerror(errno), now() - t); close(fd); return; }
	double tc = now() - t; char rb[64]; int got = 0;
	while (now() - t < 3 + tc) {
		struct pollfd pf = {fd, POLLIN, 0};
		if (poll(&pf, 1, 200) <= 0) continue;
		int r = recv(fd, rb + got, sizeof rb - 1 - got, 0);
		if (r <= 0) { lg("PROBE %d FAIL read %s dt=%.2f", k, r == 0 ? "EOF" : strerror(errno), now() - t); close(fd); return; }
		got += r; rb[got] = 0;
		if (got >= 5) { lg("PROBE %d %s resp connect=%.2f dt=%.2f", k, strncmp(rb, "HTTP/", 5) ? "FAIL" : "OK", tc, now() - t); close(fd); return; }
	}
	lg("PROBE %d FAIL read timeout dt=%.2f", k, now() - t); close(fd);
}
static int probe_main(char **argv) {
	const char *H = argv[2]; int P = atoi(argv[3]); const char *hh = argv[4], *TAG = argv[5];
	double iv = atof(argv[6]) / 1000.0, end = now() + atof(argv[7]), next = now(); int k = 0;
	signal(SIGCHLD, SIG_IGN);
	lg("START probe host=%s port=%d tag=%s iv=%.1f", H, P, TAG, iv);
	while (now() < end && access("stop", F_OK) != 0) {
		if (now() < next) { usleep(50000); continue; }
		next += iv; k++;
		if (fork() == 0) { probe1(H, P, hh, k); _exit(0); }
	}
	lg("END probe n=%d", k);
	return 0;
}
int main(int argc, char **argv) {
	signal(SIGPIPE, SIG_IGN);
	if (argc >= 8 && !strcmp(argv[1], "mq")) return mq_main(argv);
	if (argc >= 8 && !strcmp(argv[1], "probe")) return probe_main(argv);
	fprintf(stderr, "usage: mqflap mq <ip> <port> <topic> <tag> <ka_s> <dur_s> | probe <ip> <port> <hosthdr> <tag> <iv_ms> <dur_s>\n");
	return 2;
}
