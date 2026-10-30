import Cocoa
import UserNotifications

let app = NSApplication.shared
app.setActivationPolicy(.accessory)
let args = CommandLine.arguments
if args.count != 3 { NSWorkspace.shared.open(URL(string:"http://127.0.0.1:18789/#chat")!); exit(0) }
let inputURL = URL(fileURLWithPath:args[1])
let outputURL = URL(fileURLWithPath:args[2])
func finish(_ value:[String:Any]) {
    if let data = try? JSONSerialization.data(withJSONObject:value) { try? data.write(to:outputURL,options:.atomic) }
    DispatchQueue.main.async { app.terminate(nil) }
}
guard let raw = try? Data(contentsOf:inputURL), let input = try? JSONSerialization.jsonObject(with:raw) as? [String:Any] else { exit(2) }
let center = UNUserNotificationCenter.current()
class Delegate:NSObject,UNUserNotificationCenterDelegate {
 func userNotificationCenter(_ center:UNUserNotificationCenter,willPresent notification:UNNotification,withCompletionHandler handler:@escaping (UNNotificationPresentationOptions)->Void) { handler([.banner,.list,.sound]) }
 func userNotificationCenter(_ center:UNUserNotificationCenter,didReceive response:UNNotificationResponse,withCompletionHandler handler:@escaping ()->Void) {
  NSWorkspace.shared.open(URL(string:"http://127.0.0.1:18789/#chat")!);handler()
 }
}
let delegate=Delegate();center.delegate=delegate
func settings(_ then:@escaping (UNNotificationSettings)->Void) { center.getNotificationSettings(completionHandler:then) }
let action=input["action"] as? String ?? "status"
if action == "authorize" {
 center.requestAuthorization(options:[.alert,.sound]) { allowed,error in
  finish(["status":allowed ? "authorized" : "denied","error":error == nil ? NSNull() : "AUTHORIZATION_FAILED"])
 }
} else if action == "remove" {
 let ids=input["ids"] as? [String] ?? []
 center.removePendingNotificationRequests(withIdentifiers:ids);center.removeDeliveredNotifications(withIdentifiers:ids);finish(["status":"removed"])
} else {
 settings { s in
  let valid=s.authorizationStatus == .authorized || s.authorizationStatus == .provisional
  if action == "status" {
   center.getDeliveredNotifications { rows in finish(["status":valid ? "authorized" : s.authorizationStatus == .notDetermined ? "not_determined" : "denied","alert":s.alertSetting == .enabled,"notification_center":s.notificationCenterSetting == .enabled,"delivered_ids":rows.map{$0.request.identifier}]) }
  } else if action == "deliver",valid,let id=input["id"] as? String,let body=input["body"] as? String {
   center.getDeliveredNotifications { existing in
    if existing.contains(where:{$0.request.identifier==id}) { finish(["status":"delivered","id":id,"deduplicated":true]);return }
    let content=UNMutableNotificationContent();content.title="海辛";content.body=body;content.sound = .default
    let request=UNNotificationRequest(identifier:id,content:content,trigger:nil)
    center.add(request) { error in
     if error != nil { finish(["status":"failed","error":"NOTIFICATION_REJECTED"]);return }
     DispatchQueue.main.asyncAfter(deadline:.now()+0.7) {
      center.getDeliveredNotifications { rows in finish(["status":rows.contains(where:{$0.request.identifier==id}) ? "delivered" : "accepted","id":id]) }
     }
    }
   }
  } else { finish(["status":valid ? "invalid_input" : s.authorizationStatus == .notDetermined ? "not_determined" : "denied"]) }
 }
}
DispatchQueue.main.asyncAfter(deadline:.now()+55) { finish(["status":"timeout"]) }
app.run()
