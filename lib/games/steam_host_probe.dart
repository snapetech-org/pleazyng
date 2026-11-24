import 'dart:async';
import 'dart:io';
import 'dart:math';

import 'game_models.dart';
import 'steam_discovery_codec.dart';

Future<List<InternetAddress>> _lookup(String address) =>
    InternetAddress.lookup(address, type: InternetAddressType.IPv4);

/// Ask the configured Steam computer for its identity. A hostname can resolve
/// to several addresses, including disconnected interfaces. Only accept an
/// answer from a resolved address on the requested port.
Future<SteamAdvertisement> probeSteamHost(
  GameHost host, {
  required Duration lookupTimeout,
  required Duration replyTimeout,
  String? expectedId,
  Future<List<InternetAddress>> Function(String address) resolve = _lookup,
}) async {
  final answers = await resolve(host.address).timeout(lookupTimeout);
  final addresses = answers.where((address) => address.type == InternetAddressType.IPv4).toSet().take(8).toSet();
  if (addresses.isEmpty) throw const FormatException('Computer unavailable');

  final socket = await RawDatagramSocket.bind(InternetAddress.anyIPv4, 0);
  final result = Completer<SteamAdvertisement>();
  final subscription = socket.listen(
    (event) {
      if (event != RawSocketEvent.read) return;
      for (var count = 0; count < 64; count++) {
        final packet = socket.receive();
        if (packet == null) break;
        if (packet.port != host.port || !addresses.contains(packet.address)) continue;
        final status = SteamDiscoveryCodec.decode(packet.data);
        if (status == null || (expectedId != null && status.id != expectedId)) continue;
        result.complete(status);
        socket.close();
        break;
      }
    },
    onError: (Object error, StackTrace trace) {
      if (!result.isCompleted) result.completeError(error, trace);
    },
  );
  try {
    final query = SteamDiscoveryCodec.query(Random.secure().nextInt(0x7fffffff) + 1);
    for (final address in addresses) {
      socket.send(query, address, host.port);
    }
    return await result.future.timeout(replyTimeout);
  } finally {
    socket.close();
    await subscription.cancel();
  }
}
