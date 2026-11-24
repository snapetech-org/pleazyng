part of 'bif_thumbnail_service.dart';

/// https://developer.roku.com/dev/docs/bif-file-creation
/// Version 0 stores sorted (timestamp, byte offset) pairs after its 64-byte header.
class _BifIndex {
  _BifIndex(this.data, this.count, this.multiplier, this.firstFrame);

  final ByteData data;
  final int count, multiplier, firstFrame;
  int _lastFrame = -1;
  Uint8List? _lastBytes;

  static _BifIndex? parse(Uint8List bytes) {
    if (bytes.length < 72) return null;
    final data = ByteData.sublistView(bytes);
    if (data.getUint32(0) != 0x89424946 || data.getUint32(4) != 0x0d0a1a0a || data.getUint32(8, Endian.little) != 0) {
      return null;
    }
    final count = data.getUint32(12, Endian.little);
    final sentinel = 64 + count * 8;
    if (sentinel + 8 > bytes.length || data.getUint32(sentinel, Endian.little) != 0xffffffff) return null;
    int? firstFrame;
    var previousTime = -1;
    var offset = data.getUint32(68, Endian.little);
    if (offset < sentinel + 8) return null;
    for (var i = 0; i < count; i++) {
      final row = 64 + i * 8;
      final time = data.getUint32(row, Endian.little);
      final next = data.getUint32(row + 12, Endian.little);
      if (time < previousTime || time == 0xffffffff || next < offset || next > bytes.length) return null;
      if (next > offset) firstFrame ??= i;
      previousTime = time;
      offset = next;
    }
    final multiplier = data.getUint32(16, Endian.little);
    return firstFrame == null ? null : _BifIndex(data, count, multiplier == 0 ? 1000 : multiplier, firstFrame);
  }

  int _offset(int index) => data.getUint32(68 + index * 8, Endian.little);

  Uint8List frameAt(Duration time) {
    final units = time.inMilliseconds ~/ multiplier;
    var lo = firstFrame, hi = count - 1;
    while (lo < hi) {
      final mid = (lo + hi + 1) ~/ 2;
      if (data.getUint32(64 + mid * 8, Endian.little) <= units) {
        lo = mid;
      } else {
        hi = mid - 1;
      }
    }
    final offset = _offset(lo);
    if (offset == _offset(lo + 1)) {
      // A run of empty frames shares one offset. Keep showing the last
      // actual image until the timestamp of the next nonempty frame.
      hi = lo;
      lo = firstFrame;
      while (lo < hi) {
        final mid = (lo + hi) ~/ 2;
        if (_offset(mid) < offset) {
          lo = mid + 1;
        } else {
          hi = mid;
        }
      }
      lo--;
    }
    if (lo != _lastFrame) {
      _lastFrame = lo;
      // Flutter can retain a MemoryImage after playback ends. Give it just
      // this JPEG, so one cached frame cannot pin the complete archive.
      _lastBytes = Uint8List.fromList(Uint8List.sublistView(data, _offset(lo), _offset(lo + 1)));
    }
    return _lastBytes!;
  }
}
