/// Plan-limit refusals shared by every upload path on ProjectNotifier (#121).
///
/// The uploads are raw multipart requests that report failure by returning
/// null/false, so a 402 would otherwise vanish silently. This records it
/// instead, for the dialog that ran the upload to turn into an upgrade prompt.
///
/// It deliberately does **not** set `error`: a quota is not a failure the user
/// caused, and showing the red banner as well would report it twice.
library;

import 'package:flutter/foundation.dart';

import '../api/client.dart';
import '../billing/billing_service.dart';

mixin ProjectQuotaMixin on ChangeNotifier {
  /// The last upload refused on a plan limit, until someone consumes it.
  /// Root state, held by ProjectNotifier.
  QuotaError? get quotaError;
  set quotaError(QuotaError? value);

  /// Record a refusal from a raw HTTP response. True when it was a quota 402.
  bool recordQuotaRefusal(int statusCode, String body) {
    final quota = QuotaError.fromApiException(ApiException(statusCode, body));
    if (quota == null) return false;
    quotaError = quota;
    notifyListeners();
    return true;
  }

  /// Take the pending refusal, clearing it — one 402, one prompt.
  QuotaError? takeQuotaError() {
    final quota = quotaError;
    quotaError = null;
    return quota;
  }
}
