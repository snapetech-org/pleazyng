import 'dart:convert';
import 'dart:io';
import 'package:flutter_test/flutter_test.dart';
import 'package:plezy/games/game_discovery.dart';
import 'package:plezy/games/game_models.dart';
import 'package:plezy/games/steam_discovery_codec.dart';
import 'package:plezy/games/steam_host_probe.dart';

void main() {
  test('manual Steam identity retains UDP source instead of advertised TCP port', () async {
    final server = await RawDatagramSocket.bind(InternetAddress.loopbackIPv4, 0);
    final subscription = server.listen((event) {
      if (event != RawSocketEvent.read) return;
      final request = server.receive();
      if (request == null) return;
      final name = utf8.encode('Gaming PC');
      // connect_port=1234 belongs to the separate Steam-to-Steam service.
      server.send(
        SteamDiscoveryCodec.frame([8, 42, 16, 1], [24, 0xd2, 9, 34, name.length, ...name, 48, 2]),
        request.address,
        request.port,
      );
    });
    try {
      final host = await identifySteamHost(
        GameHost(id: 'manual', name: 'PC', address: '127.0.0.1', protocol: GameProtocol.steam, port: server.port),
      );
      expect(host.id, '42');
      expect(host.port, server.port);
      expect(host.name, 'Gaming PC');
    } finally {
      server.close();
      await subscription.cancel();
    }
  });

  test('Steam probe checks other resolved addresses and waits for the selected computer', () async {
    final server = await RawDatagramSocket.bind(InternetAddress.loopbackIPv4, 0);
    final subscription = server.listen((event) {
      if (event != RawSocketEvent.read) return;
      final request = server.receive();
      if (request == null) return;
      final name = utf8.encode('Gaming PC');
      final body = [34, name.length, ...name, 48, 2];
      for (final id in [41, 42]) {
        server.send(SteamDiscoveryCodec.frame([8, id, 16, 1], body), request.address, request.port);
      }
    });
    try {
      final result = await probeSteamHost(
        GameHost(id: '42', name: 'PC', address: 'steam-host.test', protocol: GameProtocol.steam, port: server.port),
        lookupTimeout: const Duration(seconds: 1),
        replyTimeout: const Duration(seconds: 1),
        expectedId: '42',
        resolve: (_) async => [InternetAddress('127.0.0.2'), InternetAddress.loopbackIPv4],
      );
      expect(result.id, '42');
      expect(result.name, 'Gaming PC');
    } finally {
      server.close();
      await subscription.cancel();
    }
  });
}
