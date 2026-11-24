import 'dart:async';
import 'dart:convert';
import 'dart:io';
import 'dart:typed_data';

import 'package:flutter_test/flutter_test.dart';
import 'package:plezy/games/game_models.dart';
import 'package:plezy/games/steam_stream_preferences.dart';
import 'package:plezy/games/steam_internet_tunnel.dart';
import 'package:plezy/multiview/steam_game_workspace_surface.dart';
import 'package:plezy/utils/certificate_trust.dart';

import 'steam_tunnel_codec_test.dart' show identity;

class _LocalTLS extends HttpOverrides {
  _LocalTLS(this.context);
  final SecurityContext context;
  @override
  HttpClient createHttpClient(SecurityContext? _) => super.createHttpClient(context);
}

class _Relay {
  _Relay({this.upgrade = true, this.sendReady = true});

  final bool upgrade, sendReady;
  final requested = Completer<void>();
  late HttpServer server;
  late RawDatagramSocket observer;
  final peers = <String, WebSocket>{};
  final proxies = <String, RawDatagramSocket>{};
  final sources = <String, Datagram>{};
  bool dropUdp = false;
  int forwarded = 0;
  final wire = <Uint8List>[];
  Uri get uri => Uri.parse('wss://127.0.0.1:${server.port}/games/steam/relay');
  Future<SecurityContext> start() async {
    final context = SecurityContext(withTrustedRoots: false)
      ..useCertificateChain('test/fixtures/steam_relay/cert.pem')
      ..usePrivateKey('test/fixtures/steam_relay/key.pem');
    final trusted = SecurityContext(withTrustedRoots: false)
      ..setTrustedCertificates('test/fixtures/steam_relay/cert.pem');
    observer = await RawDatagramSocket.bind(InternetAddress.loopbackIPv4, 0);
    for (final role in ['host', 'receiver']) {
      final proxy = await RawDatagramSocket.bind(InternetAddress.loopbackIPv4, 0);
      proxies[role] = proxy;
      proxy.listen((event) {
        if (event != RawSocketEvent.read) return;
        final d = proxy.receive();
        if (d == null || dropUdp) return;
        final other = role == 'host' ? 'receiver' : 'host', destination = sources[role];
        if (destination != null) {
          proxies[other]!.send(d.data, destination.address, destination.port);
        }
      });
    }
    observer.listen((event) {
      if (event != RawSocketEvent.read) return;
      final d = observer.receive();
      if (d == null || d.data.length != 36) return;
      final role = d.data[4] == 1 ? 'host' : 'receiver';
      if (sources.containsKey(role)) return;
      sources[role] = d;
      peers[role]?.add(jsonEncode({'kind': 'observed', 'ip': '127.0.0.1', 'port': proxies[role]!.port}));
    });
    server = await HttpServer.bindSecure(InternetAddress.loopbackIPv4, 0, context);
    server.listen((request) async {
      if (!requested.isCompleted) requested.complete();
      expect(request.headers.value('X-Steam-Relay-Admission'), 'a' * 43);
      expect(request.headers.value('Authorization'), startsWith('Bearer '));
      expect(request.uri.hasQuery, isFalse);
      if (!upgrade) return;
      final role = request.headers.value('X-Steam-Relay-Role')!;
      peers[role] = await WebSocketTransformer.upgrade(request, compression: CompressionOptions.compressionOff);
      final socket = peers[role]!; // Owned and closed by the fixture's peer map.
      if (sendReady) {
        socket.add(
          jsonEncode({
            'kind': 'ready',
            'udpPort': observer.port,
            'udpCookie': base64Url.encode(List.filled(32, role == 'host' ? 1 : 2)).replaceAll('=', ''),
          }),
        );
      }
      socket.listen(
        (data) {
          expect(data, isA<List<int>>());
          forwarded++;
          if (wire.length < 10) wire.add(Uint8List.fromList(data as List<int>));
          peers[role == 'host' ? 'receiver' : 'host']?.add(data);
        },
        onDone: () {
          unawaited(socket.close());
          peers.remove(role);
          for (final peer in peers.values) {
            unawaited(peer.close());
          }
        },
      );
      if (peers.length == 2) {
        for (final peer in peers.values) {
          peer.add('{"kind":"peer"}');
        }
      }
    });
    return trusted;
  }

  Future<void> close() async {
    for (final peer in peers.values.toList()) {
      unawaited(peer.close());
    }
    observer.close();
    for (final p in proxies.values) {
      p.close();
    }
    await server.close(force: true);
  }
}

Future<_LocalTLS> _prepareRelay(_Relay relay) async {
  final trusted = await relay.start();
  CertificateTrust.debugSetUserAuthoritiesForTesting(trusted);
  addTearDown(CertificateTrust.debugReset);
  addTearDown(relay.close);
  return _LocalTLS(trusted);
}

Future<void> until(bool Function() condition) async {
  final deadline = DateTime.now().add(const Duration(seconds: 12));
  while (!condition()) {
    if (DateTime.now().isAfter(deadline)) fail('route did not settle');
    await Future<void>.delayed(const Duration(milliseconds: 40));
  }
}

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();
  test('candidate and deployment URLs reject private destinations and unexpected origins', () {
    for (final s in [
      '127.0.0.1',
      '10.0.0.1',
      '100.64.1.1',
      '169.254.169.254',
      '192.0.2.1',
      '198.18.1.1',
      '224.0.0.1',
      '::1',
    ]) {
      expect(steamPublicIPv4(InternetAddress(s)), isFalse, reason: s);
    }
    expect(steamPublicIPv4(InternetAddress('1.1.1.1')), isTrue);
    for (final s in [
      '',
      'http://ice.plezy.app',
      'https://user@ice.plezy.app',
      'https://other.test',
      'https://ice.plezy.app?token=x',
    ]) {
      expect(steamRelayUri(s), isNull);
    }
  });
  test('retiring a tunnel cancels an in-flight WebSocket upgrade', () async {
    final relay = _Relay(upgrade: false);
    final tls = await _prepareRelay(relay);
    var allowed = true;
    final pending = HttpOverrides.runWithHttpOverrides(
      () => SteamInternetTunnel.connectForTesting(
        identity(),
        host: false,
        allowed: () => allowed,
        admission: 'a' * 43,
        relay: relay.uri,
        isPublic: (address) => address.isLoopback,
      ),
      tls,
    );
    final result = expectLater(pending.timeout(const Duration(seconds: 3)), throwsA(isA<SocketException>()));
    await relay.requested.future.timeout(const Duration(seconds: 2));
    allowed = false;
    await result;
  });

  test('a host only waits indefinitely after the relay acknowledges readiness', () async {
    Future<SteamInternetTunnel> connectHost(_Relay relay) async {
      final tls = await _prepareRelay(relay);
      final host = await HttpOverrides.runWithHttpOverrides(
        () => SteamInternetTunnel.connectForTesting(
          identity(),
          host: true,
          allowed: () => true,
          admission: 'a' * 43,
          relay: relay.uri,
          isPublic: (address) => address.isLoopback,
          waitForPeer: false,
        ),
        tls,
      );
      addTearDown(() {
        host.close();
        host.path.dispose();
      });
      return host;
    }

    final ready = await connectHost(_Relay());
    final waiting = await connectHost(_Relay(sendReady: false));
    await waiting.done.timeout(const Duration(seconds: 10));
    expect(waiting.current, isFalse);
    expect(ready.current, isTrue);
  });

  test('real TLS relay, encrypted UDP upgrade/fallback and permission retirement carry Steam datagrams', () async {
    final relay = _Relay()..dropUdp = true;
    final tls = await _prepareRelay(relay);
    await HttpOverrides.runWithHttpOverrides(() async {
      var allowed = true;
      // Ephemeral fixture ports keep the real Steam client and parallel tests intact.
      final steamSockets = <RawDatagramSocket>[];
      for (var i = 0; i < 6; i++) {
        final socket = await RawDatagramSocket.bind(InternetAddress.loopbackIPv4, 0);
        steamSockets.add(socket);
        addTearDown(socket.close);
      }
      final host = await SteamInternetTunnel.connectForTesting(
        identity(),
        host: true,
        allowed: () => allowed,
        admission: 'a' * 43,
        relay: relay.uri,
        isPublic: (a) => a.isLoopback,
        waitForPeer: false,
        hostPorts: steamSockets.map((socket) => socket.port).toList(),
      );
      addTearDown(() {
        host.close();
        host.path.dispose();
      });
      final receiver = await SteamInternetTunnel.connectForTesting(
        identity(),
        host: false,
        allowed: () => allowed,
        admission: 'a' * 43,
        relay: relay.uri,
        isPublic: (a) => a.isLoopback,
      );
      final computer = GameHost(id: '123', name: 'Desktop', address: 'desktop.local', protocol: GameProtocol.steam);
      final session = SteamGameWorkspaceSession(
        host: computer,
        game: StreamGame(hostKey: computer.key, id: '620', name: 'Portal 2'),
        touchControls: false,
        quality: SteamStreamQuality.ultraHd,
        pyrowave: true,
        allowHdr: true,
        internetTunnel: receiver,
      );
      addTearDown(session.dispose);
      expect(session.creationParams['hostId'], '123');
      expect(session.creationParams['hostKey'], 'steam:123');
      expect(session.creationParams['address'], '127.0.0.1');
      expect(session.creationParams['port'], receiver.ports[5]);
      expect(session.creationParams['relayPort0'], receiver.ports[0]);
      expect(session.creationParams['pyrowave'], isTrue);
      expect(session.creationParams['allowHdr'], isTrue);
      expect(receiver.relayBitrateCeilingKbps, 50331); // Older relay without a rate field.
      expect(session.creationParams['streamBitrateKbps'], 50331);
      expect(session.creationParams['streamWidth'], 3840);
      final steam = steamSockets[5];
      final native = await RawDatagramSocket.bind(InternetAddress.loopbackIPv4, 0);
      addTearDown(native.close);
      Completer<List<int>>? awaitingReply;
      final subscription = steam.listen((event) {
        if (event != RawSocketEvent.read) return;
        final d = steam.receive();
        if (d != null) steam.send([...d.data, 99], d.address, d.port);
      });
      addTearDown(subscription.cancel);
      final nativeSub = native.listen((event) {
        if (event != RawSocketEvent.read) return;
        final d = native.receive();
        final pending = awaitingReply;
        if (d != null && pending != null && !pending.isCompleted) pending.complete(d.data);
      });
      addTearDown(nativeSub.cancel);
      Future<void> echo(int tag) async {
        final pending = awaitingReply = Completer<List<int>>();
        try {
          native.send([11, 22, 33, 44, tag], InternetAddress.loopbackIPv4, receiver.ports[5]);
          expect(await pending.future.timeout(const Duration(seconds: 4)), [11, 22, 33, 44, tag, 99]);
        } finally {
          awaitingReply = null;
        }
      }

      await echo(1);
      expect(receiver.path.value, SteamInternetPath.relay);
      relay.dropUdp = false;
      await until(() => host.path.value == SteamInternetPath.direct && receiver.path.value == SteamInternetPath.direct);
      expect(session.internetPath.value, SteamInternetPath.direct);
      final before = relay.forwarded;
      await echo(2);
      expect(relay.forwarded, before);
      relay.dropUdp = true;
      await until(() => host.path.value == SteamInternetPath.relay && receiver.path.value == SteamInternetPath.relay);
      expect(session.internetPath.value, SteamInternetPath.relay);
      await echo(3);
      expect(relay.forwarded, greaterThan(before));
      expect(relay.wire.every((p) => !p.join(',').contains('11,22,33,44')), isTrue);
      allowed = false;
      await Future.wait([host.done, receiver.done]).timeout(const Duration(seconds: 2));
      expect(session.sessionState.value, 'failed');
      expect(session.internetPath.value, isNull);
      expect(session.creationParams['address'], computer.address);
      expect(session.creationParams.containsKey('relayPort0'), isFalse);
    }, tls);
  });
}
